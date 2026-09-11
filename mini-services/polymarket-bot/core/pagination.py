"""Cursor-based pagination for list endpoints.

W16-5 — Cursor pagination utilities shared by every list endpoint that
returns a potentially large dataset (trades, events, closed positions,
decision rejections, audit logs, alerts).

Design
~~~~~~

Cursors are opaque base64-encoded JSON blobs carrying a ``(timestamp, id)``
tuple. The pair forms a *stable* pagination key:

  * Stable across inserts — even when a brand-new row lands at the head of
    the feed between two paginated requests, the second request's cursor
    points at the LAST row of the previous page (a fixed point in the
    ``ORDER BY (timestamp DESC, id DESC)`` sequence), so the next page
    picks up exactly where the previous one left off. Offset-based
    pagination cannot do this — a single new row at the head shifts every
    subsequent offset by one.

  * Stable across reconnects — the cursor is a self-contained descriptor
    of the boundary row; the caller does not need to remember the page
    number, sort direction, or filter shape.

Three helpers cover the three distinct list shapes in this codebase:

  (1) ``paginate_query``   — SQLite-backed lists where the SQL is built
                              up from a base ``SELECT`` + ``WHERE``
                              clause. Used by ``closed_positions``,
                              ``decision_rejections``, ``audit_events``,
                              and ``alerts``. Adds the cursor condition
                              + ``ORDER BY`` + ``LIMIT`` to the base
                              query and fetches one extra row to detect
                              ``has_more`` without a separate ``COUNT(*)``.

  (2) ``paginate_list``    — In-memory lists of *records* (objects that
                              expose a stable ``(timestamp, id)`` pair
                              via a caller-supplied ``key_fn``). Used by
                              ``/api/trades`` (``store.trades`` is a list
                              of ``Trade`` objects, each carrying
                              ``timestamp`` + ``trade_id``).

  (3) ``paginate_offset`` — In-memory lists of *opaque* items (bare
                              strings or unsortable dicts). Falls back to
                              offset-based pagination encoded inside the
                              same opaque cursor. Used by ``/api/events``
                              (the event-log entries are bare strings of
                              the form ``"[HH:MM:SS] message"`` with no
                              natural id field; the list is already
                              newest-first via ``store.log_event``'s
                              append-then-trim pattern, so an offset into
                              that list IS a stable cursor for as long
                              as no new rows are appended between
                              requests — acceptable for the in-memory
                              event log use case).

The returned ``Page`` dataclass always carries ``items``, ``next_cursor``,
and ``has_more``. Routes that wrap a ``Page`` typically also include the
existing ``count`` field (mirroring ``len(items)``) for backward
compatibility with the pre-pagination response shape.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence


# ── Page dataclass ───────────────────────────────────────────────────────────


@dataclass
class Page:
    """A single page of paginated results.

    Attributes:
        items:        The list of items on this page (already sliced to
                      ``limit`` length). Each item is whatever the
                      underlying store returned (a ``dict`` for SQLite
                      rows via ``sqlite3.Row``; the original object for
                      in-memory lists).
        next_cursor:  Opaque cursor to pass to the next request to fetch
                      the following page. ``None`` when this is the last
                      page (``has_more`` is False).
        prev_cursor:  Reserved for future bidirectional pagination.
                      Always ``None`` today (the API only paginates
                      forward — newest first — which covers every
                      existing caller). Kept on the dataclass so a
                      future caller can populate it without breaking
                      the wire contract.
        has_more:     True when at least one more row exists beyond
                      this page. The pagination helpers fetch
                      ``limit + 1`` rows and set ``has_more`` based on
                      whether the extra row was present, so the value
                      is exact (not a heuristic).
        total_count:  Optional total row count (only populated when the
                      caller explicitly asks for it via a separate
                      ``COUNT(*)`` query — fetching the total on every
                      page request would defeat the point of
                      cursor-based pagination, which exists precisely
                      to avoid the full table scan a ``COUNT(*)``
                      requires on large tables).
    """

    items: list = field(default_factory=list)
    next_cursor: Optional[str] = None
    prev_cursor: Optional[str] = None
    has_more: bool = False
    total_count: Optional[int] = None


# ── Cursor encode / decode ──────────────────────────────────────────────────


def encode_cursor(timestamp: float, record_id: str = "") -> str:
    """Encode a ``(timestamp, id)`` boundary into an opaque base64 cursor.

    The cursor is a JSON blob ``{"ts": <float>, "id": "<str>"}`` wrapped
    in urlsafe base64. ``record_id`` is coerced to ``str`` so callers
    can pass ``int`` PKs (``sqlite3``'s ``INTEGER PRIMARY KEY`` columns)
    without an explicit cast at the call site.

    The blob is NOT encrypted — it's only opaque to a casual reader. The
    cursor is round-trip-stable: ``decode_cursor(encode_cursor(ts, id))``
    returns ``(ts, id)`` exactly. Malformed blobs decode to ``(0, "")``
    (see :func:`decode_cursor`) so a tampered cursor never crashes a
    request — it just restarts pagination from the beginning.
    """
    data = json.dumps({"ts": float(timestamp), "id": str(record_id)})
    return base64.urlsafe_b64encode(data.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[float, str]:
    """Decode an opaque cursor back to ``(timestamp, id)``.

    Returns ``(0.0, "")`` on ANY decoding failure (malformed base64,
    malformed JSON, missing keys, wrong types). The fallback is the
    "beginning of the feed" cursor — pagination restarts from the
    newest row, which is the safest default for a public-facing API
    endpoint: a tampered cursor can never crash the request, only
    rewind it.
    """
    if not cursor:
        return 0.0, ""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        ts = float(data.get("ts", 0) or 0)
        rid = str(data.get("id", "") or "")
        return ts, rid
    except Exception:  # noqa: BLE001 — any decode failure → safe restart
        return 0.0, ""


def encode_offset_cursor(offset: int) -> str:
    """Encode a list-offset into the same opaque cursor format.

    Used by :func:`paginate_offset` for in-memory lists whose items
    don't expose a natural ``(timestamp, id)`` key (e.g. the bare-string
    event log entries on ``/api/events``). The cursor still LOOKS like
    the standard ``(ts, id)`` blob to outside callers — they don't need
    to know that ``ts`` is being repurposed as a list offset and
    ``id`` is empty. This keeps the wire contract uniform across every
    paginated endpoint.
    """
    data = json.dumps({"ts": float(offset), "id": ""})
    return base64.urlsafe_b64encode(data.encode("utf-8")).decode("ascii")


def decode_offset_cursor(cursor: str) -> int:
    """Decode an offset cursor. Returns 0 on any failure (restart)."""
    ts, _ = decode_cursor(cursor)
    try:
        return max(0, int(ts))
    except (TypeError, ValueError):
        return 0


# ── SQLite-backed pagination ────────────────────────────────────────────────


def paginate_query(
    conn: Any,
    query: str,
    params: tuple = (),
    cursor: Optional[str] = None,
    limit: int = 50,
    cursor_column: str = "timestamp",
    id_column: str = "id",
    reverse: bool = True,
) -> Page:
    """Apply cursor-based pagination to a SQL query.

    Args:
        conn:           Open SQLite connection. ``conn.row_factory`` should
                         be ``sqlite3.Row`` so column-name access works;
                         this helper tolerates plain tuples too (falls
                         back to position 0/1 for the cursor columns).
        query:          Base ``SELECT`` query (without ``ORDER BY`` /
                         ``LIMIT``). MUST end with a ``WHERE`` clause
                         (even if it's just ``WHERE 1=1``) so the cursor
                         condition can be appended with a leading ``AND``.
        params:         Positional params for the base query's ``WHERE``
                         clause. The cursor condition's params are
                         appended to this list internally.
        cursor:         Opaque cursor from the previous page's
                         ``next_cursor`` field. ``None`` (or an empty
                         string) returns the first page.
        limit:          Page size. Clamped to ``[1, 100]`` — a hard
                         ceiling that protects the database from
                         adversarial callers asking for ``limit=10000``.
        cursor_column:  Column used for the primary sort key. Defaults
                         to ``"timestamp"`` — every relevant table in
                         this codebase (``audit_events``,
                         ``decision_events``, ``decision_rejections``,
                         ``closed_positions``, ``alerts``) has a
                         ``timestamp`` column.
        id_column:      Tie-breaker column for rows that share a
                         ``cursor_column`` value (e.g. rows written in
                         the same millisecond). Defaults to ``"id"`` —
                         every relevant table has an ``INTEGER PRIMARY
                         KEY`` ``id`` column that monotonically
                         increases with insert order, so the
                         ``(timestamp, id)`` pair is a strict total
                         order over the rows.
        reverse:        If True (default), newest rows first. The
                         cursor condition becomes
                         ``(ts < ? OR (ts = ? AND id < ?))`` and the
                         ``ORDER BY`` is ``DESC``. If False, oldest
                         first; the cursor condition becomes
                         ``(ts > ? OR (ts = ? AND id > ?))`` and the
                         ``ORDER BY`` is ``ASC``.

    Returns:
        A :class:`Page` with the rows (as ``dict`` for ``sqlite3.Row``
        inputs, or the raw tuple otherwise) sliced to ``limit`` length
        plus the ``next_cursor`` + ``has_more`` flags.
    """
    limit = min(max(int(limit), 1), 100)

    cursor_params: list[Any] = list(params)
    cursor_condition = ""
    if cursor:
        ts, rid = decode_cursor(cursor)
        if reverse:
            cursor_condition = (
                f" AND ({cursor_column} < ? "
                f"OR ({cursor_column} = ? AND {id_column} < ?))"
            )
            cursor_params.extend([ts, ts, rid])
        else:
            cursor_condition = (
                f" AND ({cursor_column} > ? "
                f"OR ({cursor_column} = ? AND {id_column} > ?))"
            )
            cursor_params.extend([ts, ts, rid])

    order = "DESC" if reverse else "ASC"
    paginated_sql = (
        f"{query} {cursor_condition} "
        f"ORDER BY {cursor_column} {order}, {id_column} {order} "
        f"LIMIT ?"
    )
    cursor_params.append(limit + 1)  # fetch one extra to detect has_more

    rows = conn.execute(paginated_sql, cursor_params).fetchall()

    has_more = len(rows) > limit
    items = list(rows[:limit])

    next_cursor: Optional[str] = None
    if has_more and items:
        last = items[-1]
        # sqlite3.Row exposes .keys() and supports name-based access.
        # Plain tuples fall back to position 0 (cursor_column) + 1 (id).
        if hasattr(last, "keys"):
            last_dict = dict(last)
            ts = last_dict.get(cursor_column)
            rid = last_dict.get(id_column, "")
        else:
            ts = last[0] if len(last) > 0 else None
            rid = last[1] if len(last) > 1 else ""
        next_cursor = encode_cursor(
            float(ts) if ts is not None else 0.0,
            str(rid) if rid is not None else "",
        )

    # Normalize items: sqlite3.Row → dict; plain tuples stay as-is.
    normalized = [
        dict(r) if hasattr(r, "keys") else r for r in items
    ]
    return Page(
        items=normalized,
        next_cursor=next_cursor,
        has_more=has_more,
    )


# ── In-memory record-list pagination ────────────────────────────────────────


def paginate_list(
    items: Sequence[Any],
    cursor: Optional[str] = None,
    limit: int = 50,
    key_fn: Optional[Callable[[Any], tuple[float, str]]] = None,
    reverse: bool = True,
) -> Page:
    """Cursor-based pagination for an in-memory list of *records*.

    Used when the items expose a stable ``(timestamp, id)`` pair (via a
    caller-supplied ``key_fn``). The list is sorted by that pair, the
    cursor condition is applied, and the slice is returned.

    Args:
        items:   Sequence of records (objects or dicts).
        cursor:  Opaque cursor from the previous page. ``None`` returns
                 the first page.
        limit:   Page size (clamped to ``[1, 100]``).
        key_fn:  Callable ``(item) -> (timestamp: float, id: str)``.
                 Used both for sorting AND for extracting the
                 ``next_cursor`` from the last item on the page. If
                 ``None``, the items are assumed to already be sorted in
                 the desired order and the cursor uses the list index
                 as the position key (falls back to
                 :func:`paginate_offset` semantics — fragile, only use
                 when the list truly is already sorted and stable).
        reverse: True (default) for newest-first. The list is sorted
                 ``reverse=True`` (descending) and the cursor filter
                 keeps items strictly *before* the cursor.

    Returns:
        :class:`Page` with the slice + ``next_cursor`` + ``has_more``.
    """
    limit = min(max(int(limit), 1), 100)

    if key_fn is not None:
        # Sort by the natural key so the cursor is meaningful even when
        # the input list isn't pre-sorted (e.g. trades appended in
        # arrival order, which is usually — but not guaranteed —
        # timestamp order).
        ordered = sorted(items, key=key_fn, reverse=reverse)
    else:
        # Caller asserts the list is already sorted; honor the direction.
        ordered = list(reversed(items)) if reverse else list(items)

    if cursor:
        ts, rid = decode_cursor(cursor)
        if key_fn is not None:
            cursor_key = (ts, rid)
            if reverse:
                ordered = [
                    it for it in ordered
                    if _tuple_key(key_fn(it)) < cursor_key
                ]
            else:
                ordered = [
                    it for it in ordered
                    if _tuple_key(key_fn(it)) > cursor_key
                ]
        else:
            # Without a key_fn the cursor encodes a list offset.
            offset = decode_offset_cursor(cursor)
            ordered = ordered[offset:]

    has_more = len(ordered) > limit
    page_items = list(ordered[:limit])

    next_cursor: Optional[str] = None
    if has_more and page_items:
        last = page_items[-1]
        if key_fn is not None:
            last_ts, last_id = key_fn(last)
            next_cursor = encode_cursor(float(last_ts), str(last_id))
        else:
            # Offset cursor: encode the position one past the last
            # returned item so the next page starts exactly there.
            next_cursor = encode_offset_cursor(len(page_items))

    return Page(
        items=page_items,
        next_cursor=next_cursor,
        has_more=has_more,
    )


# ── In-memory offset-list pagination ────────────────────────────────────────


def paginate_offset(
    items: Sequence[Any],
    cursor: Optional[str] = None,
    limit: int = 50,
) -> Page:
    """Offset-based pagination for in-memory lists without natural keys.

    Used by ``/api/events`` — the event-log entries are bare strings of
    the form ``"[HH:MM:SS] message"`` with no id field; the list is
    already newest-first (``store.log_event`` appends then trims the
    head when the cap is hit, so the LAST element IS the newest). An
    offset into this list is a stable cursor for as long as no new rows
    are appended between requests — acceptable for the in-memory event
    log use case (events stream continuously anyway, so any pagination
    scheme would surface "missed" rows on a long enough gap between
    requests; offset pagination makes this explicit rather than
    pretending to be stable).

    Args:
        items:  Sequence of opaque items (strings, dicts without id).
        cursor: Opaque cursor from the previous page. ``None`` returns
                the first page (newest first).
        limit:  Page size (clamped to ``[1, 100]``).

    Returns:
        :class:`Page` with ``items``, ``next_cursor``, and ``has_more``.
    """
    limit = min(max(int(limit), 1), 100)
    offset = decode_offset_cursor(cursor) if cursor else 0

    # ``items`` is assumed to be newest-first (the natural order for an
    # event log): items[0] is the newest. We slice [offset:offset+limit]
    # and report has_more based on the residual length.
    page_items = list(items[offset: offset + limit])
    has_more = (offset + limit) < len(items)

    next_cursor: Optional[str] = None
    if has_more and page_items:
        next_cursor = encode_offset_cursor(offset + len(page_items))

    return Page(
        items=page_items,
        next_cursor=next_cursor,
        has_more=has_more,
    )


# ── FastAPI request-param helper ────────────────────────────────────────────


def parse_pagination_params(
    limit: int = 50,
    cursor: Optional[str] = None,
) -> dict[str, Any]:
    """Parse + validate the standard pagination query params.

    Returns a dict suitable for ``**kwargs`` splat into
    :func:`paginate_query` / :func:`paginate_list` /
    :func:`paginate_offset`:

        {"limit": <clamped int>, "cursor": <str or None>}

    ``limit`` is clamped to ``[1, 100]``. ``cursor`` is passed through
    unchanged (an empty string is normalized to ``None`` so the helpers'
    ``if cursor:`` guard works).
    """
    return {
        "limit": min(max(int(limit), 1), 100),
        "cursor": cursor or None,
    }


# ── Internal helpers ─────────────────────────────────────────────────────────


def _tuple_key(pair: tuple[float, str]) -> tuple[float, str]:
    """Coerce a ``(timestamp, id)`` pair to comparable types.

    Defensive: if ``key_fn`` returns a pair with a ``None`` timestamp
    (e.g. an old trade record missing the field), substitute 0.0 so the
    sort + filter operations don't blow up on ``None < float``.
    """
    ts, rid = pair
    return (float(ts) if ts is not None else 0.0, str(rid) if rid is not None else "")


__all__ = [
    "Page",
    "encode_cursor",
    "decode_cursor",
    "encode_offset_cursor",
    "decode_offset_cursor",
    "paginate_query",
    "paginate_list",
    "paginate_offset",
    "parse_pagination_params",
]
