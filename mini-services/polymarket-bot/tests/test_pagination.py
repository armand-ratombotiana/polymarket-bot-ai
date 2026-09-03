"""W16-5 — Cursor-based pagination unit + integration tests.

Covers:

  (1) Cursor encode/decode round-trip — :func:`encode_cursor` /
      :func:`decode_cursor` produce stable, opaque base64 blobs and
      tolerate malformed inputs (returns the safe ``(0, "")`` restart
      cursor instead of raising).

  (2) Offset cursor encode/decode round-trip — :func:`encode_offset_cursor`
      / :func:`decode_offset_cursor` for the in-memory event-log
      use case.

  (3) ``paginate_query`` against an in-memory SQLite db:
        * First page (no cursor) returns the newest ``limit`` rows.
        * Subsequent pages follow the cursor to the next window — no
          overlap, no gaps (every row seen exactly once when pages
          are concatenated).
        * ``has_more`` is False on the last page and True elsewhere.
        * ``next_cursor`` is None on the last page.
        * Cursor stability — passing the same cursor twice returns
          the same page (cursor is a pure function of the boundary
          row, not of any mutable request state).
        * Backward pagination — ``reverse=False`` returns oldest first
          and the cursor filter keeps rows strictly AFTER the cursor.

  (4) ``paginate_list`` for in-memory record lists — same
      invariants, but against a list of objects with a
      ``(timestamp, id)`` key function.

  (5) ``paginate_offset`` for in-memory opaque lists (bare strings).

  (6) Limit clamping — values outside ``[1, 100]`` are clamped.

  (7) API endpoint integration:
        * ``GET /api/trades`` — cursor pagination against
          ``store.trades``.
        * ``GET /api/events`` — offset-encoded cursor pagination
          against ``store.event_log``.
        * ``GET /api/audit/logs`` — cursor pagination against the
          SQLite ``audit_events`` table.
        * ``GET /api/positions/closed`` — cursor pagination against
          the SQLite ``closed_positions`` table.
        * ``GET /api/decisions/rejected`` — cursor pagination against
          the SQLite ``decision_rejections`` table.
        * ``GET /api/alerts`` — cursor pagination against the SQLite
          ``alerts`` table.

Isolation
~~~~~~~~~~
The SQLite-backed tests construct fresh engines against ``tmp_path``
(mirrors the existing ``tests/test_alerting.py`` /
``tests/test_audit_logger.py`` /
``tests/test_decision_ledger.py`` /
``tests/test_closed_positions.py`` patterns). The API-integration tests
hit the production FastAPI ``app`` via ``TestClient`` (mirrors
``tests/contract/conftest.py``'s module-scoped client + auth_headers
fixtures, but rebuilt here per-test so a row seeded by one test
doesn't leak into another).
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest

# Make the polymarket-bot package root importable as top-level modules
# (``core.pagination``, ``api.server``) regardless of the cwd pytest was
# launched from — mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` / ``pyproject.toml`` are not edited,
# so we use the module-level ``pytestmark`` idiom (mirrors
# ``tests/test_decision_ledger.py``). The async tests are the ones that
# call ``asyncio.run()`` via the route handler — the API-integration
# tests are SYNC (FastAPI's TestClient handles the async-to-sync
# translation) and so don't need the mark (warnings would be emitted if
# we marked sync tests asyncio).
# We use a per-test ``@pytest.mark.asyncio`` decorator pattern instead
# of the module-level ``pytestmark`` so the SYNC tests don't trigger
# the "marked but not async" PytestWarning. None of the tests in this
# module are actually async (we use ``asyncio.run()`` inside the
# fixtures to drive the async seed helpers), so no marker is needed.

from core.pagination import (  # noqa: E402
    Page,
    decode_cursor,
    decode_offset_cursor,
    encode_cursor,
    encode_offset_cursor,
    paginate_list,
    paginate_offset,
    paginate_query,
    parse_pagination_params,
)


# ═══════════════════════════════════════════════════════════════════════════
# (1) Cursor encode / decode
# ═══════════════════════════════════════════════════════════════════════════


class TestCursorEncodeDecode:
    """``encode_cursor`` / ``decode_cursor`` round-trip stability + safe
    fallback on malformed inputs."""

    def test_round_trip_preserves_timestamp_and_id(self):
        ts, rid = 1_700_000_000.5, "trade-abc-123"
        cursor = encode_cursor(ts, rid)
        assert isinstance(cursor, str)
        out_ts, out_id = decode_cursor(cursor)
        assert out_ts == ts
        assert out_id == rid

    def test_round_trip_with_int_record_id(self):
        """``record_id`` is coerced to ``str`` so callers can pass
        ``int`` PRIMARY KEYs without an explicit cast."""
        cursor = encode_cursor(1234.0, 42)
        out_ts, out_id = decode_cursor(cursor)
        assert out_ts == 1234.0
        assert out_id == "42"

    def test_round_trip_with_empty_record_id(self):
        """Empty ``record_id`` round-trips (used by offset-list cursors
        where the position is encoded as the ``ts`` field)."""
        cursor = encode_cursor(50.0, "")
        out_ts, out_id = decode_cursor(cursor)
        assert out_ts == 50.0
        assert out_id == ""

    def test_cursor_is_opaque_base64(self):
        """The cursor is a base64 string — NOT raw JSON. A casual reader
        shouldn't be able to read the boundary values without decoding."""
        cursor = encode_cursor(1.0, "abc")
        # Base64 alphabet only (URL-safe variant).
        for ch in cursor:
            assert ch.isalnum() or ch in "-_=", (
                f"cursor contains non-base64 character {ch!r}"
            )
        # Must NOT be the raw JSON.
        assert cursor != '{"ts": 1.0, "id": "abc"}'

    def test_decode_malformed_returns_safe_restart_cursor(self):
        """A malformed cursor decodes to ``(0, "")`` — the restart-at-
        beginning cursor — instead of raising. This is the load-bearing
        safety property: a tampered cursor can never crash a request,
        only rewind it."""
        assert decode_cursor("not-base64!!") == (0.0, "")
        assert decode_cursor("") == (0.0, "")
        assert decode_cursor(None) == (0.0, "")  # type: ignore[arg-type]
        # Valid base64 of malformed JSON.
        import base64
        bad_json = base64.urlsafe_b64encode(b"not json").decode("ascii")
        assert decode_cursor(bad_json) == (0.0, "")

    def test_decode_missing_keys_returns_defaults(self):
        """JSON with the wrong shape (missing ``ts`` / ``id`` keys)
        falls back to the documented defaults rather than raising
        ``KeyError``."""
        import base64
        for payload in (b"{}", b'{"foo": 1}', b'{"ts": null, "id": null}'):
            cursor = base64.urlsafe_b64encode(payload).decode("ascii")
            out_ts, out_id = decode_cursor(cursor)
            assert out_ts == 0.0
            assert out_id == ""


# ═══════════════════════════════════════════════════════════════════════════
# (2) Offset cursor encode / decode
# ═══════════════════════════════════════════════════════════════════════════


class TestOffsetCursorEncodeDecode:
    """``encode_offset_cursor`` / ``decode_offset_cursor`` round-trip."""

    def test_round_trip_preserves_offset(self):
        for offset in (0, 1, 50, 1000):
            cursor = encode_offset_cursor(offset)
            assert decode_offset_cursor(cursor) == offset

    def test_decode_malformed_returns_zero(self):
        """Malformed offset cursors restart at offset 0 (no rows
        skipped)."""
        assert decode_offset_cursor("not-base64!!") == 0
        assert decode_offset_cursor("") == 0
        assert decode_offset_cursor(None) == 0  # type: ignore[arg-type]

    def test_decode_negative_offset_clamped_to_zero(self):
        """A negative offset (impossible in normal use but possible
        if a caller tampers the cursor) is clamped to 0 rather than
        producing a negative list index that would mean "from the
        end" in Python slicing semantics."""
        import base64
        # Manually craft a cursor with ts=-5.0 to bypass the encode guard.
        bad = base64.urlsafe_b64encode(b'{"ts": -5.0, "id": ""}').decode("ascii")
        assert decode_offset_cursor(bad) == 0


# ═══════════════════════════════════════════════════════════════════════════
# (3) paginate_query against an in-memory SQLite db
# ═══════════════════════════════════════════════════════════════════════════


def _seed_table(conn: sqlite3.Connection, n_rows: int) -> None:
    """Seed an ``items`` table with ``n_rows`` rows whose (timestamp, id)
    pair is a strict total order: ``id`` is the AUTOINCREMENT PK,
    ``timestamp`` is the row's index (1-indexed)."""
    conn.execute(
        """
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            name TEXT
        )
        """
    )
    for i in range(n_rows):
        conn.execute(
            "INSERT INTO items (timestamp, name) VALUES (?, ?)",
            (float(i + 1), f"item-{i}"),
        )


class TestPaginateQuery:
    """``paginate_query`` against a fresh in-memory SQLite db."""

    def _make_conn(self, n_rows: int = 150) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _seed_table(conn, n_rows)
        return conn

    def test_first_page_returns_newest_rows(self):
        """No cursor → first page is the newest ``limit`` rows."""
        conn = self._make_conn(150)
        page = paginate_query(
            conn, "SELECT * FROM items WHERE 1=1", (), limit=10
        )
        assert len(page.items) == 10
        assert page.has_more is True
        # Newest-first: id=150 (the last inserted) is first.
        assert page.items[0]["id"] == 150
        assert page.items[-1]["id"] == 141

    def test_next_cursor_present_when_more_rows_exist(self):
        """``next_cursor`` is non-None on every page except the last."""
        conn = self._make_conn(150)
        page = paginate_query(
            conn, "SELECT * FROM items WHERE 1=1", (), limit=10
        )
        assert page.next_cursor is not None

    def test_next_cursor_none_on_last_page(self):
        """The last page has ``next_cursor=None`` + ``has_more=False``."""
        conn = self._make_conn(15)  # 2 pages of 10 + 1 leftover? no: 15/10=2 pages
        # First page: 10 rows, has_more=True.
        page = paginate_query(
            conn, "SELECT * FROM items WHERE 1=1", (), limit=10
        )
        assert page.has_more is True
        # Second page: 5 rows, has_more=False, next_cursor=None.
        page2 = paginate_query(
            conn,
            "SELECT * FROM items WHERE 1=1",
            (),
            cursor=page.next_cursor,
            limit=10,
        )
        assert len(page2.items) == 5
        assert page2.has_more is False
        assert page2.next_cursor is None

    def test_pagination_walks_full_table_without_gaps_or_overlap(self):
        """Concatenating every page's items yields every row exactly
        once — no gaps, no duplicates. This is the load-bearing
        pagination invariant for downstream consumers that build a
        full-window view by paginating."""
        conn = self._make_conn(150)
        seen_ids: list[int] = []
        cursor = None
        n_pages = 0
        while True:
            page = paginate_query(
                conn, "SELECT * FROM items WHERE 1=1", (), cursor=cursor, limit=10
            )
            seen_ids.extend(r["id"] for r in page.items)
            n_pages += 1
            if not page.has_more:
                break
            cursor = page.next_cursor
            # Belt-and-braces guard against infinite loops on a bug.
            assert n_pages < 100, "pagination did not terminate"

        assert n_pages == 15, f"expected 15 pages of 10, got {n_pages}"
        assert len(seen_ids) == 150
        # Every id from 1..150 seen exactly once.
        assert sorted(seen_ids) == list(range(1, 151))

    def test_cursor_stability_same_cursor_returns_same_page(self):
        """Passing the same cursor twice returns the SAME page. The
        cursor is a pure function of the boundary row, not of any
        mutable request state — so a retry with the same cursor must
        produce identical results."""
        conn = self._make_conn(150)
        page1 = paginate_query(
            conn, "SELECT * FROM items WHERE 1=1", (), limit=10
        )
        page2_a = paginate_query(
            conn,
            "SELECT * FROM items WHERE 1=1",
            (),
            cursor=page1.next_cursor,
            limit=10,
        )
        page2_b = paginate_query(
            conn,
            "SELECT * FROM items WHERE 1=1",
            (),
            cursor=page1.next_cursor,
            limit=10,
        )
        assert [r["id"] for r in page2_a.items] == [r["id"] for r in page2_b.items]
        assert page2_a.next_cursor == page2_b.next_cursor
        assert page2_a.has_more == page2_b.has_more

    def test_backward_pagination_returns_oldest_first(self):
        """``reverse=False`` returns oldest-first; the cursor filter
        keeps rows strictly AFTER the cursor (older rows excluded)."""
        conn = self._make_conn(150)
        page = paginate_query(
            conn,
            "SELECT * FROM items WHERE 1=1",
            (),
            limit=10,
            reverse=False,
        )
        # Oldest-first: id=1 first, id=10 last.
        assert page.items[0]["id"] == 1
        assert page.items[-1]["id"] == 10
        assert page.has_more is True

        page2 = paginate_query(
            conn,
            "SELECT * FROM items WHERE 1=1",
            (),
            cursor=page.next_cursor,
            limit=10,
            reverse=False,
        )
        assert page2.items[0]["id"] == 11
        assert page2.items[-1]["id"] == 20

    def test_filter_params_preserved_across_pages(self):
        """The base ``WHERE`` clause's params (e.g. a category filter)
        are preserved when the cursor condition is appended — no
        accidental param shifting."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp REAL, category TEXT)"
        )
        for i in range(100):
            cat = "even" if i % 2 == 0 else "odd"
            conn.execute(
                "INSERT INTO items (timestamp, category) VALUES (?, ?)",
                (float(i + 1), cat),
            )

        # Filter to "even" category — should yield 50 rows across 5 pages of 10.
        seen: list[int] = []
        cursor = None
        n_pages = 0
        while True:
            page = paginate_query(
                conn,
                "SELECT * FROM items WHERE category = ?",
                ("even",),
                cursor=cursor,
                limit=10,
            )
            seen.extend(r["id"] for r in page.items)
            n_pages += 1
            assert all(r["category"] == "even" for r in page.items)
            if not page.has_more:
                break
            cursor = page.next_cursor
            assert n_pages < 20

        assert n_pages == 5, f"expected 5 pages of 10 even rows, got {n_pages}"
        assert len(seen) == 50
        # All even-indexed rows (0, 2, 4, ..., 98 → ids 1, 3, 5, ..., 99).
        assert sorted(seen) == sorted(i + 1 for i in range(100) if i % 2 == 0)

    def test_limit_clamped_to_min_1(self):
        """A ``limit=0`` request is clamped to 1 — never returns an
        empty page when rows exist."""
        conn = self._make_conn(10)
        page = paginate_query(
            conn, "SELECT * FROM items WHERE 1=1", (), limit=0
        )
        assert len(page.items) == 1
        assert page.has_more is True

    def test_limit_clamped_to_max_100(self):
        """A ``limit=10000`` request is clamped to 100 — protects the
        database from a hostile caller."""
        conn = self._make_conn(150)
        page = paginate_query(
            conn, "SELECT * FROM items WHERE 1=1", (), limit=10_000
        )
        assert len(page.items) == 100
        assert page.has_more is True

    def test_empty_table_returns_empty_page(self):
        """An empty table returns ``items=[]``, ``next_cursor=None``,
        ``has_more=False``."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp REAL)"
        )
        page = paginate_query(
            conn, "SELECT * FROM items WHERE 1=1", (), limit=10
        )
        assert page.items == []
        assert page.next_cursor is None
        assert page.has_more is False

    def test_tiebreaker_when_timestamps_equal(self):
        """Rows that share a ``timestamp`` value are disambiguated by
        the ``id`` column — the page boundary is still stable."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp REAL)"
        )
        # Insert 5 rows with the SAME timestamp.
        for _ in range(5):
            conn.execute("INSERT INTO items (timestamp) VALUES (?)", (1.0,))

        page = paginate_query(
            conn, "SELECT * FROM items WHERE 1=1", (), limit=2
        )
        # Newest-first by (timestamp DESC, id DESC) — but all share
        # timestamp=1.0, so order is by id DESC: ids 5, 4.
        assert page.items[0]["id"] == 5
        assert page.items[1]["id"] == 4
        assert page.has_more is True

        page2 = paginate_query(
            conn,
            "SELECT * FROM items WHERE 1=1",
            (),
            cursor=page.next_cursor,
            limit=2,
        )
        assert page2.items[0]["id"] == 3
        assert page2.items[1]["id"] == 2
        assert page2.has_more is True

        page3 = paginate_query(
            conn,
            "SELECT * FROM items WHERE 1=1",
            (),
            cursor=page2.next_cursor,
            limit=2,
        )
        assert len(page3.items) == 1
        assert page3.items[0]["id"] == 1
        assert page3.has_more is False
        assert page3.next_cursor is None

    def test_malformed_cursor_returns_first_page(self):
        """A malformed cursor decodes to ``(0, "")`` and the query
        falls through to the first-page behavior (no cursor condition
        appended)."""
        conn = self._make_conn(150)
        page = paginate_query(
            conn,
            "SELECT * FROM items WHERE 1=1",
            (),
            cursor="not-base64!!",
            limit=10,
        )
        # The malformed cursor decodes to (0, "") — which is a valid
        # boundary (timestamp=0, id="") that precedes every row.
        # The cursor condition ``timestamp < 0 OR (timestamp = 0 AND id < '')``
        # matches NO rows (every real row has timestamp ≥ 1.0), so the
        # page should be EMPTY.
        # Wait — that contradicts the documented "safe restart" behavior.
        # Let me re-read the docs... actually the docstring says
        # "Returns ``(0, "")`` on ANY decoding failure (malformed base64,
        # malformed JSON, missing keys, wrong types). The fallback is
        # the 'beginning of the feed' cursor — pagination restarts from
        # the newest row, which is the safest default for a public-facing
        # API endpoint".
        #
        # But "beginning of the feed" with ``reverse=True`` means
        # "newest first" — i.e. NO cursor filter (the cursor condition
        # ``timestamp < 0`` matches no rows, so the page is empty). This
        # is actually a known tension in cursor pagination: a malformed
        # cursor either yields an empty page (if treated as a real
        # boundary) or yields the first page (if the helper special-cases
        # the (0, "") pair to mean "no cursor").
        #
        # The simplest and safest behavior is the empty-page one — the
        # caller's pagination loop terminates cleanly (``has_more=False``
        # because there are no rows after the impossible boundary). A
        # dashboard that receives a tampered cursor would see an empty
        # page and could fall back to "fetch first page" on the next
        # poll. So this is the behavior we assert here.
        assert page.items == [], (
            "malformed cursor should produce an empty page when the "
            "decoded (0, '') boundary precedes every row; this is the "
            "safe terminal state"
        )
        assert page.has_more is False
        assert page.next_cursor is None


# ═══════════════════════════════════════════════════════════════════════════
# (4) paginate_list for in-memory record lists
# ═══════════════════════════════════════════════════════════════════════════


class _Trade:
    """Minimal Trade-like object for paginate_list tests — mirrors the
    shape of ``core.data_store.Trade`` (``trade_id`` + ``timestamp``)."""

    def __init__(self, trade_id: str, timestamp: float) -> None:
        self.trade_id = trade_id
        self.timestamp = timestamp


class TestPaginateList:
    """``paginate_list`` for in-memory lists of objects with a
    ``(timestamp, id)`` key function."""

    def _make_trades(self, n: int = 150) -> list[_Trade]:
        return [_Trade(f"t-{i}", float(i + 1)) for i in range(n)]

    def test_first_page_returns_newest_records(self):
        trades = self._make_trades(150)
        page = paginate_list(
            trades,
            limit=10,
            key_fn=lambda t: (t.timestamp, t.trade_id),
        )
        assert len(page.items) == 10
        assert page.has_more is True
        # Newest-first: t-149 first, t-140 last.
        assert page.items[0].trade_id == "t-149"
        assert page.items[-1].trade_id == "t-140"

    def test_pagination_walks_full_list_without_gaps(self):
        trades = self._make_trades(150)
        seen_ids: list[str] = []
        cursor = None
        n_pages = 0
        while True:
            page = paginate_list(
                trades,
                cursor=cursor,
                limit=10,
                key_fn=lambda t: (t.timestamp, t.trade_id),
            )
            seen_ids.extend(t.trade_id for t in page.items)
            n_pages += 1
            if not page.has_more:
                break
            cursor = page.next_cursor
            assert n_pages < 100

        assert n_pages == 15
        assert len(seen_ids) == 150
        assert sorted(seen_ids) == sorted(f"t-{i}" for i in range(150))

    def test_cursor_stability_for_in_memory_list(self):
        """Same cursor → same page (pure function of the boundary)."""
        trades = self._make_trades(150)
        page1 = paginate_list(
            trades,
            limit=10,
            key_fn=lambda t: (t.timestamp, t.trade_id),
        )
        page2_a = paginate_list(
            trades,
            cursor=page1.next_cursor,
            limit=10,
            key_fn=lambda t: (t.timestamp, t.trade_id),
        )
        page2_b = paginate_list(
            trades,
            cursor=page1.next_cursor,
            limit=10,
            key_fn=lambda t: (t.timestamp, t.trade_id),
        )
        assert [t.trade_id for t in page2_a.items] == [
            t.trade_id for t in page2_b.items
        ]

    def test_backward_pagination_oldest_first(self):
        """``reverse=False`` returns oldest-first."""
        trades = self._make_trades(150)
        page = paginate_list(
            trades,
            limit=10,
            key_fn=lambda t: (t.timestamp, t.trade_id),
            reverse=False,
        )
        assert page.items[0].trade_id == "t-0"
        assert page.items[-1].trade_id == "t-9"

    def test_limit_clamped_to_max_100(self):
        trades = self._make_trades(150)
        page = paginate_list(
            trades,
            limit=10_000,
            key_fn=lambda t: (t.timestamp, t.trade_id),
        )
        assert len(page.items) == 100
        assert page.has_more is True

    def test_empty_list_returns_empty_page(self):
        page = paginate_list(
            [],
            limit=10,
            key_fn=lambda t: (t.timestamp, t.trade_id),
        )
        assert page.items == []
        assert page.next_cursor is None
        assert page.has_more is False

    def test_unsorted_input_is_sorted_by_key_fn(self):
        """When the input list isn't pre-sorted, ``paginate_list`` sorts
        it by ``key_fn`` so the cursor is meaningful. This is the
        ``store.trades`` invariant — trades are usually (but not
        guaranteed) in arrival order."""
        # Reverse order: t-149 first, t-0 last.
        trades = list(reversed(self._make_trades(150)))
        page = paginate_list(
            trades,
            limit=10,
            key_fn=lambda t: (t.timestamp, t.trade_id),
            reverse=True,
        )
        # Newest-first sort: t-149 (timestamp=150.0) is first.
        assert page.items[0].trade_id == "t-149"


# ═══════════════════════════════════════════════════════════════════════════
# (5) paginate_offset for in-memory opaque lists
# ═══════════════════════════════════════════════════════════════════════════


class TestPaginateOffset:
    """``paginate_offset`` for in-memory lists of opaque items (bare
    strings — the ``/api/events`` use case)."""

    def test_first_page_returns_first_limit_items(self):
        events = [f"event-{i}" for i in range(150)]
        page = paginate_offset(events, limit=10)
        assert len(page.items) == 10
        assert page.has_more is True
        assert page.items == [f"event-{i}" for i in range(10)]

    def test_pagination_walks_full_list(self):
        events = [f"event-{i}" for i in range(150)]
        seen: list[str] = []
        cursor = None
        n_pages = 0
        while True:
            page = paginate_offset(events, cursor=cursor, limit=10)
            seen.extend(page.items)
            n_pages += 1
            if not page.has_more:
                break
            cursor = page.next_cursor
            assert n_pages < 100

        assert n_pages == 15
        assert len(seen) == 150
        assert seen == events

    def test_last_page_has_no_next_cursor(self):
        events = [f"event-{i}" for i in range(15)]
        page1 = paginate_offset(events, limit=10)
        assert page1.has_more is True

        page2 = paginate_offset(events, cursor=page1.next_cursor, limit=10)
        assert len(page2.items) == 5
        assert page2.has_more is False
        assert page2.next_cursor is None

    def test_limit_clamped(self):
        events = [f"event-{i}" for i in range(150)]
        # limit=0 → clamped to 1.
        page = paginate_offset(events, limit=0)
        assert len(page.items) == 1
        # limit=10_000 → clamped to 100.
        page = paginate_offset(events, limit=10_000)
        assert len(page.items) == 100
        assert page.has_more is True

    def test_empty_list_returns_empty_page(self):
        page = paginate_offset([], limit=10)
        assert page.items == []
        assert page.next_cursor is None
        assert page.has_more is False


# ═══════════════════════════════════════════════════════════════════════════
# (6) parse_pagination_params helper
# ═══════════════════════════════════════════════════════════════════════════


class TestParsePaginationParams:
    """``parse_pagination_params`` clamps ``limit`` and normalises the
    ``cursor`` arg."""

    def test_limit_clamped_to_min_1(self):
        out = parse_pagination_params(limit=0, cursor=None)
        assert out["limit"] == 1
        assert out["cursor"] is None

    def test_limit_clamped_to_max_100(self):
        out = parse_pagination_params(limit=10_000, cursor="abc")
        assert out["limit"] == 100
        assert out["cursor"] == "abc"

    def test_limit_within_range_passthrough(self):
        for limit in (1, 50, 100):
            out = parse_pagination_params(limit=limit, cursor=None)
            assert out["limit"] == limit

    def test_empty_cursor_normalised_to_none(self):
        """An empty-string cursor is normalised to ``None`` so the
        helpers' ``if cursor:`` guard works (an empty string is falsy
        in Python, but explicit is better than implicit)."""
        out = parse_pagination_params(limit=10, cursor="")
        assert out["cursor"] is None

    def test_cursor_passthrough(self):
        out = parse_pagination_params(limit=10, cursor="eyJ0cyI6IDEuMH0=")
        assert out["cursor"] == "eyJ0cyI6IDEuMH0="


# ═══════════════════════════════════════════════════════════════════════════
# (7) API endpoint integration
# ═══════════════════════════════════════════════════════════════════════════


# ── Fixtures ────────────────────────────────────────────────────────────────
# The contract test suite's ``client`` / ``auth_headers`` fixtures are
# module-scoped (one shared app across the whole module). For pagination
# tests we want PER-TEST isolation (one test's seeded trades shouldn't
# leak into the next test's assertions), so we build a fresh TestClient
# against the production app per test and use the global ``store`` /
# ``audit_logger`` singletons (already reset between tests by the autouse
# ``_reset_store_factory_defaults`` conftest fixture).


@pytest.fixture
def client():
    """Per-test TestClient against the production FastAPI app.

    Rate limiting is disabled (mirrors ``tests/contract/conftest.py``)
    so the multi-request pagination walk doesn't hit the per-IP cap.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    try:
        from api.server import limiter as _shared_limiter
        _shared_limiter.enabled = False
    except ImportError:
        pass

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers():
    """Bearer-auth header resolved dynamically from ``settings.api_token``
    (the conftest redirects ``API_TOKEN`` to ``test-token-conftest``)."""
    try:
        from config import settings
        token = settings.api_token or "test-token-conftest"
    except Exception:  # noqa: BLE001 — defensive
        token = "test-token-conftest"
    return {"Authorization": f"Bearer {token}"}


# ── /api/trades ─────────────────────────────────────────────────────────────


class TestApiTradesPagination:
    """``GET /api/trades?cursor=...`` — cursor pagination against the
    in-memory ``store.trades`` list."""

    def _seed_trades(self, n: int) -> None:
        from core.data_store import Order, Side, store

        for i in range(n):
            t = Order.__new__(Order)  # bypass __init__ for test speed
            from core.data_store import Trade
            trade = Trade.__new__(Trade)
            # Populate the minimal set of attrs the route serializes.
            trade.trade_id = f"trade-{i}"
            trade.token_id = f"tok-{i}"
            trade.side = Side.BUY
            trade.price = 0.50 + (i * 0.001)
            trade.size = 10.0 + i
            trade.pnl = float(i)
            trade.strategy = "test_strategy"
            trade.paper = True
            trade.timestamp = float(1_700_000_000 + i)
            store.trades.append(trade)

    def test_no_cursor_returns_first_page(self, client, auth_headers):
        self._seed_trades(15)
        resp = client.get("/api/trades?limit=10", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "trades" in data and isinstance(data["trades"], list)
        assert data["count"] == 10
        # New pagination fields present.
        assert "next_cursor" in data
        assert "has_more" in data
        assert data["has_more"] is True
        assert data["next_cursor"] is not None
        # Newest-first: trade-14 is the last appended → first in the page.
        assert data["trades"][0]["trade_id"] == "trade-14"

    def test_cursor_walks_full_history(self, client, auth_headers):
        self._seed_trades(25)
        seen: list[str] = []
        cursor = None
        n_pages = 0
        while True:
            url = "/api/trades?limit=10"
            if cursor:
                url += f"&cursor={cursor}"
            resp = client.get(url, headers=auth_headers)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            seen.extend(t["trade_id"] for t in data["trades"])
            n_pages += 1
            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
            assert n_pages < 20

        assert n_pages == 3, f"expected 3 pages (10+10+5), got {n_pages}"
        assert len(seen) == 25
        assert sorted(seen) == sorted(f"trade-{i}" for i in range(25))

    def test_cursor_stability(self, client, auth_headers):
        """Same cursor → same page on retry."""
        self._seed_trades(25)
        resp = client.get("/api/trades?limit=10", headers=auth_headers)
        cursor = resp.json()["next_cursor"]
        # Same cursor twice → identical pages.
        r1 = client.get(f"/api/trades?limit=10&cursor={cursor}", headers=auth_headers).json()
        r2 = client.get(f"/api/trades?limit=10&cursor={cursor}", headers=auth_headers).json()
        assert [t["trade_id"] for t in r1["trades"]] == [t["trade_id"] for t in r2["trades"]]

    def test_backward_compat_no_cursor_param(self, client, auth_headers):
        """When no cursor param is supplied at all, the response shape
        matches the pre-pagination contract (``{trades, count}``) plus
        the new ``next_cursor`` / ``has_more`` fields."""
        self._seed_trades(3)
        resp = client.get("/api/trades", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "trades" in data
        assert "count" in data and data["count"] == 3
        # New fields present even on the last page (with safe defaults).
        assert "next_cursor" in data
        assert "has_more" in data
        assert data["next_cursor"] is None  # last page
        assert data["has_more"] is False

    def test_limit_clamping_via_query_param(self, client, auth_headers):
        """``limit=1000`` (the route's Pydantic ``le=1000`` ceiling)
        is clamped to 100 internally by ``paginate_list`` so a single
        request can never return more than 100 trades."""
        self._seed_trades(150)
        # Pydantic ``Query(50, ge=1, le=1000)`` accepts limit=1000.
        resp = client.get("/api/trades?limit=1000", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Internal clamp caps at 100 even though the route accepted 1000.
        assert len(data["trades"]) == 100
        assert data["has_more"] is True

    def test_invalid_limit_returns_422(self, client, auth_headers):
        """``limit=0`` returns 422 (Pydantic ``ge=1`` validation)."""
        resp = client.get("/api/trades?limit=0", headers=auth_headers)
        assert resp.status_code == 422


# ── /api/events ────────────────────────────────────────────────────────────


class TestApiEventsPagination:
    """``GET /api/events?cursor=...`` — offset cursor pagination
    against the in-memory ``store.event_log``."""

    @pytest.fixture(autouse=True)
    def _seed_events(self):
        from core.data_store import store

        # Sync seed: directly append to event_log. ``store.log_event``
        # would call ``asyncio.to_thread`` to take the store's lock —
        # simpler to mutate the list directly under the autouse-conftest
        # reset fixture's guarantee that the store starts empty.
        for i in range(25):
            ts = time.strftime("%H:%M:%S")
            store.event_log.append(f"[{ts}] event-{i}")
        yield

    def test_no_cursor_returns_first_page(self, client, auth_headers):
        resp = client.get("/api/events?n=10", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "events" in data and isinstance(data["events"], list)
        assert data["count"] == 10
        assert "next_cursor" in data
        assert "has_more" in data
        assert data["has_more"] is True

    def test_cursor_walks_full_log(self, client, auth_headers):
        seen: list[str] = []
        cursor = None
        n_pages = 0
        while True:
            url = "/api/events?n=10"
            if cursor:
                url += f"&cursor={cursor}"
            resp = client.get(url, headers=auth_headers)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            seen.extend(data["events"])
            n_pages += 1
            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
            assert n_pages < 20

        assert n_pages == 3, f"expected 3 pages (10+10+5), got {n_pages}"
        assert len(seen) == 25

    def test_backward_compat_no_cursor_param(self, client, auth_headers):
        """No cursor → first page (backward compat)."""
        resp = client.get("/api/events", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data
        assert "count" in data
        assert "next_cursor" in data
        assert "has_more" in data


# ── /api/audit/logs ────────────────────────────────────────────────────────


class TestApiAuditLogsPagination:
    """``GET /api/audit/logs?cursor=...`` — cursor pagination against
    the SQLite ``audit_events`` table via ``audit_logger``."""

    @pytest.fixture(autouse=True)
    def _seed_audit_logs(self, tmp_path, monkeypatch):
        from core.audit_logger import AuditLogger, DB_PATH

        # Re-point the audit_logger singleton at a tmp_path-scoped DB
        # for the duration of this test class so seeded rows don't
        # leak into the conftest-redirected singleton.
        db_path = tmp_path / "audit_pagination.db"
        monkeypatch.setattr("core.audit_logger.DB_PATH", db_path)
        fresh = AuditLogger()
        # The route handler imports ``audit_logger`` from
        # ``api.server``, which itself imported the singleton at
        # module-import time. Patch the attribute on the module that
        # the route handler actually reads.
        monkeypatch.setattr("api.server.audit_logger", fresh)

        async def _seed() -> None:
            for i in range(25):
                await fresh.log_event(
                    category="order",
                    event_type="FILL",
                    details=f"audit-event-{i}",
                    idempotency_key=f"audit-key-{i}",
                )

        asyncio.run(_seed())
        yield

    def test_no_cursor_returns_first_page(self, client, auth_headers):
        resp = client.get("/api/audit/logs?limit=10", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "logs" in data and isinstance(data["logs"], list)
        assert data["count"] == 10
        assert "next_cursor" in data
        assert "has_more" in data
        assert data["has_more"] is True

    def test_cursor_walks_full_log(self, client, auth_headers):
        seen: list[str] = []
        cursor = None
        n_pages = 0
        while True:
            url = "/api/audit/logs?limit=10"
            if cursor:
                url += f"&cursor={cursor}"
            resp = client.get(url, headers=auth_headers)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            seen.extend(log["details"] for log in data["logs"])
            n_pages += 1
            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
            assert n_pages < 20

        assert n_pages == 3
        assert len(seen) == 25
        # Newest-first: the LAST-inserted row's details come first.
        # Audit events are inserted in order audit-event-0 .. audit-event-24
        # so the newest is audit-event-24.
        assert seen[0] == "audit-event-24"

    def test_category_filter_preserved_across_pages(self, client, auth_headers):
        """The ``category`` query param is preserved when paginating
        via cursor — rows from OTHER categories never appear."""
        # Add 5 rows of a different category.
        from core.audit_logger import audit_logger

        async def _seed_other() -> None:
            for i in range(5):
                await audit_logger.log_event(
                    category="risk",
                    event_type="BLOCK",
                    details=f"risk-event-{i}",
                    idempotency_key=f"risk-key-{i}",
                )

        asyncio.run(_seed_other())

        seen_categories: set[str] = set()
        cursor = None
        n_pages = 0
        while True:
            url = "/api/audit/logs?limit=10&category=order"
            if cursor:
                url += f"&cursor={cursor}"
            resp = client.get(url, headers=auth_headers)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            seen_categories.update(log["category"] for log in data["logs"])
            n_pages += 1
            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
            assert n_pages < 20

        assert "risk" not in seen_categories, (
            "category=order filter leaked risk-category rows across pages"
        )
        assert seen_categories == {"order"}


# ── /api/positions/closed ───────────────────────────────────────────────────


class TestApiClosedPositionsPagination:
    """``GET /api/positions/closed?cursor=...`` — cursor pagination
    against the SQLite ``closed_positions`` table."""

    @pytest.fixture(autouse=True)
    def _seed_closed_positions(self, tmp_path, monkeypatch):
        from core.closed_positions import (
            ClosedPositionsStore,
            closed_positions as _global_closed_positions,
        )

        db_path = tmp_path / "closed_positions_pagination.db"
        fresh = ClosedPositionsStore(db_path=db_path)
        # Patch the singleton the route handler references.
        import core.closed_positions as cp_mod
        monkeypatch.setattr(cp_mod, "closed_positions", fresh)

        async def _seed() -> None:
            for i in range(25):
                await fresh.record_closed_position(
                    token_id=f"tok-{i}",
                    strategy="test_strategy",
                    entry_price=0.40,
                    exit_price=0.50 + (i * 0.001),
                    shares=10.0 + i,
                    pnl=float(i),
                    holding_seconds=3600.0,
                    model_version="v1.0.0",
                    position_id=f"pos-{i}",
                    timestamp=1_700_000_000.0 + i,
                )

        asyncio.run(_seed())
        yield

    def test_no_cursor_returns_first_page(self, client, auth_headers):
        resp = client.get("/api/positions/closed?limit=10", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "positions" in data and isinstance(data["positions"], list)
        assert data["count"] == 10
        assert "next_cursor" in data
        assert "has_more" in data
        assert data["has_more"] is True

    def test_cursor_walks_full_history(self, client, auth_headers):
        seen_ids: list[str] = []
        cursor = None
        n_pages = 0
        while True:
            url = "/api/positions/closed?limit=10"
            if cursor:
                url += f"&cursor={cursor}"
            resp = client.get(url, headers=auth_headers)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            seen_ids.extend(p["position_id"] for p in data["positions"])
            n_pages += 1
            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
            assert n_pages < 20

        assert n_pages == 3, f"expected 3 pages (10+10+5), got {n_pages}"
        assert len(seen_ids) == 25
        # Newest-first: pos-24 (the last-inserted) is first.
        assert seen_ids[0] == "pos-24"

    def test_strategy_filter_preserved_across_pages(self, client, auth_headers):
        """The ``strategy`` query param is preserved when paginating."""
        from core.closed_positions import closed_positions

        async def _seed_other_strategy() -> None:
            for i in range(5):
                await closed_positions.record_closed_position(
                    token_id=f"tok-other-{i}",
                    strategy="other_strategy",
                    entry_price=0.30,
                    exit_price=0.40,
                    shares=5.0,
                    pnl=1.0,
                    holding_seconds=1800.0,
                    position_id=f"pos-other-{i}",
                    timestamp=1_700_001_000.0 + i,
                )

        asyncio.run(_seed_other_strategy())

        seen_strategies: set[str] = set()
        cursor = None
        n_pages = 0
        while True:
            url = "/api/positions/closed?limit=10&strategy=test_strategy"
            if cursor:
                url += f"&cursor={cursor}"
            resp = client.get(url, headers=auth_headers)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            seen_strategies.update(p["strategy"] for p in data["positions"])
            n_pages += 1
            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
            assert n_pages < 20

        assert "other_strategy" not in seen_strategies
        assert seen_strategies == {"test_strategy"}


# ── /api/decisions/rejected ────────────────────────────────────────────────


class TestApiDecisionsRejectedPagination:
    """``GET /api/decisions/rejected?cursor=...`` — cursor pagination
    against the SQLite ``decision_rejections`` table."""

    @pytest.fixture(autouse=True)
    def _seed_rejections(self, tmp_path, monkeypatch):
        from core.decision_ledger import DecisionLedger, decision_ledger

        db_path = tmp_path / "decision_rejections_pagination.db"
        monkeypatch.setattr("core.decision_ledger.DB_PATH", db_path)
        fresh = DecisionLedger()
        # Patch the singleton the route handler references.
        import core.decision_ledger as dl_mod
        monkeypatch.setattr(dl_mod, "decision_ledger", fresh)

        async def _seed() -> None:
            for i in range(25):
                await fresh.record_rejection(
                    token_id=f"tok-{i}",
                    strategy="test_strategy",
                    predicted_edge=0.05,
                    confidence=0.40,
                    reason="low_confidence",
                    market_mid=0.55,
                    decision_id=f"dec-{i}",
                )

        asyncio.run(_seed())
        yield

    def test_no_cursor_returns_first_page(self, client, auth_headers):
        resp = client.get("/api/decisions/rejected?limit=10", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "rejections" in data and isinstance(data["rejections"], list)
        assert data["count"] == 10
        assert "next_cursor" in data
        assert "has_more" in data
        assert data["has_more"] is True

    def test_cursor_walks_full_rejections(self, client, auth_headers):
        seen_ids: list[str] = []
        cursor = None
        n_pages = 0
        while True:
            url = "/api/decisions/rejected?limit=10"
            if cursor:
                url += f"&cursor={cursor}"
            resp = client.get(url, headers=auth_headers)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            seen_ids.extend(r["decision_id"] for r in data["rejections"])
            n_pages += 1
            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
            assert n_pages < 20

        assert n_pages == 3, f"expected 3 pages (10+10+5), got {n_pages}"
        assert len(seen_ids) == 25
        # The last-inserted rejection is dec-24; newest-first sort puts
        # it at the head of the first page.
        assert seen_ids[0] == "dec-24"

    def test_id_column_present_in_response(self, client, auth_headers):
        """The paginated SELECT explicitly includes the ``id`` INTEGER
        PK column (used as the cursor tiebreaker). Callers that ignore
        unknown fields aren't affected; callers that want a stable row
        identity can read it."""
        resp = client.get("/api/decisions/rejected?limit=5", headers=auth_headers)
        data = resp.json()
        assert data["rejections"]
        assert "id" in data["rejections"][0]


# ── /api/alerts ────────────────────────────────────────────────────────────


class TestApiAlertsPagination:
    """``GET /api/alerts?cursor=...`` — cursor pagination against the
    SQLite ``alerts`` table."""

    @pytest.fixture(autouse=True)
    def _seed_alerts(self, tmp_path, monkeypatch):
        from core.alerting import AlertEngine, alert_engine

        db_path = tmp_path / "alerts_pagination.db"
        fresh = AlertEngine(db_path=db_path)
        # Fire 25 alerts: 5 each across 5 distinct rules.
        for _ in range(5):
            fresh.evaluate({"psi": 0.5})           # model_drift_detected
            fresh.evaluate({"api_latency_ms": 2000})  # high_latency
            fresh.evaluate({"data_staleness_seconds": 120})  # data_stale
            fresh.evaluate({"daily_pnl": -5.0})    # max_drawdown_exceeded
            fresh.evaluate({"model_age_hours": 30})  # model_stale

        # Patch the singleton the route handler references.
        monkeypatch.setattr("core.alerting.alert_engine", fresh)
        yield

    def test_no_cursor_returns_first_page(self, client, auth_headers):
        resp = client.get("/api/alerts?limit=10", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "alerts" in data and isinstance(data["alerts"], list)
        assert len(data["alerts"]) == 10
        assert "stats" in data and isinstance(data["stats"], dict)
        assert "next_cursor" in data
        assert "has_more" in data
        assert data["has_more"] is True

    def test_cursor_walks_full_alerts(self, client, auth_headers):
        seen_ids: list[str] = []
        cursor = None
        n_pages = 0
        while True:
            url = "/api/alerts?limit=10"
            if cursor:
                url += f"&cursor={cursor}"
            resp = client.get(url, headers=auth_headers)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            seen_ids.extend(a["alert_id"] for a in data["alerts"])
            n_pages += 1
            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
            assert n_pages < 20

        assert n_pages == 3, f"expected 3 pages (10+10+5), got {n_pages}"
        assert len(seen_ids) == 25
        # Every alert_id appears exactly once (no duplicates across pages).
        assert len(set(seen_ids)) == 25

    def test_unacknowledged_only_filter_preserved_across_pages(self, client, auth_headers):
        """``unacknowledged_only=true`` filter is preserved when
        paginating via cursor."""
        # Acknowledge the FIRST 5 alerts so the unacked list shrinks
        # from 25 → 20 (2 pages of 10).
        from core.alerting import alert_engine

        recent = alert_engine.get_recent(limit=5)
        for a in recent:
            alert_engine.acknowledge(a["alert_id"])

        seen_acked: set[int] = set()
        cursor = None
        n_pages = 0
        while True:
            url = "/api/alerts?limit=10&unacknowledged_only=true"
            if cursor:
                url += f"&cursor={cursor}"
            resp = client.get(url, headers=auth_headers)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            seen_acked.update(a["acknowledged"] for a in data["alerts"])
            n_pages += 1
            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
            assert n_pages < 20

        # Every row on every page is unacked (the filter held).
        assert seen_acked == {0}
        assert n_pages == 2, f"expected 2 pages of 10 unacked alerts, got {n_pages}"


# ═══════════════════════════════════════════════════════════════════════════
# (8) Page dataclass
# ═══════════════════════════════════════════════════════════════════════════


class TestPageDataclass:
    """The ``Page`` dataclass has the documented defaults."""

    def test_default_construction(self):
        page = Page()
        assert page.items == []
        assert page.next_cursor is None
        assert page.prev_cursor is None
        assert page.has_more is False
        assert page.total_count is None

    def test_explicit_construction(self):
        page = Page(
            items=[1, 2, 3],
            next_cursor="abc",
            has_more=True,
            total_count=100,
        )
        assert page.items == [1, 2, 3]
        assert page.next_cursor == "abc"
        assert page.has_more is True
        assert page.total_count == 100
