"""Market event ingestion — tracks market lifecycle.

W33-4 — Market event ingestion. Polls the Polymarket Gamma API for
market lifecycle events and records them in a dedicated SQLite store
(``market_events.db``) so an operator (or a future React panel) can
query the event timeline out-of-band.

Events
------
Six canonical event types are recognised (the ``EVENT_TYPES`` frozenset
documented below):

  1. ``MARKET_CREATED``           — a new market was listed on Polymarket.
  2. ``MARKET_SUSPENDED``         — trading was paused (active → inactive
                                    but not yet closed / resolved).
  3. ``MARKET_REOPENED``          — trading resumed (inactive → active
                                    without resolution).
  4. ``MARKET_CLOSED``            — trading halted, awaiting resolution.
  5. ``MARKET_RESOLVED``          — final outcome determined (YES / NO).
  6. ``MARKET_LIQUIDITY_CHANGED`` — significant liquidity delta
                                    (default ≥ 20% between polls).

For every event, the ingester:
  * Records ``(event_type, timestamp, market details)`` into the
    ``market_events`` SQLite table + writes the raw payload to the W31-1
    ``raw_vault`` so the event is audit-grade replayable.
  * Fires a ``core.alerting`` alert on the high-signal events
    (``MARKET_RESOLVED`` / ``MARKET_SUSPENDED`` / ``MARKET_CLOSED``) so
    an operator sees the lifecycle change immediately.
  * Triggers downstream actions on ``MARKET_RESOLVED``:
      - ``label_backfill.record_outcome(token_id, outcome)``
      - ``ml_model.update(features, outcome_yes)`` (online learning)
      - ``feature_pipeline.invalidate(token_id)`` (clear cached features)
    Each downstream action is best-effort — a transient ML stack failure
    must NEVER break the event-recording path (mirrors the
    ``raw_vault.record_observation`` best-effort contract).
  * Updates the in-memory ``_market_state`` cache so the next poll can
    diff against the prior snapshot (the cache is also persisted to the
    ``market_state`` SQLite table so a process restart picks up where
    the prior process left off).

Polling
-------
``detect_events`` is the canonical entry point — it polls the Gamma API
for the current active + resolved market sets, diffs each against the
cached prior state, and emits one event per detected transition. The
background ``_loop`` (driven by ``start`` / ``stop``) calls
``detect_events`` on a 60 s cadence so a market resolution lands in the
event log within a minute of the Gamma API publishing it.

Storage
-------
SQLite-backed (a dedicated db file at ``MARKET_EVENTS_DB_PATH``, default
``/app/data/market_events.db``) so the event timeline survives process
restarts and can be queried out-of-band by an operator / dashboard.
Mirrors the persistence convention of the sibling ``ingestion.raw_vault``
/ ``ingestion.lineage`` / ``ingestion.dead_letter`` modules — a
dedicated SQLite file separate from every other store so an ingestion
hiccup can never perturb the event audit trail. The constructor falls
back to ``/tmp/market_events.db`` when the default path is unwritable
(read-only sandbox) so the bot still boots.

Concurrency
-----------
The ingester is single-threaded by design — every call site
(``detect_events`` from the background loop, ``record_event`` from
sibling modules) is either a sync caller or a single ``asyncio`` task.
SQLite's default ``BEGIN IMMEDIATE`` transaction gives serialisability
across threads, and the ``check_same_thread=False`` flag lets the
ingester be shared between the asyncio loop and ``asyncio.to_thread``
offloads without a ``ProgrammingError``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

# Default DB path. The conftest redirects this in tests; production runs
# resolve it to ``/app/data/market_events.db`` (the docker-compose volume
# bind-mount makes ``/app/data`` writable in the prod container).
DEFAULT_DB_PATH = "/app/data/market_events.db"

# Polling cadence for the background loop (seconds). 60 s matches the
# Gamma API's typical metadata-update lag — polling faster would just
# burn rate-limit budget without catching resolutions any sooner.
POLL_INTERVAL_SECONDS = 60.0

# Liquidity-change threshold. A market whose reported ``liquidity``
# changes by ≥ 20% between polls fires a ``MARKET_LIQUIDITY_CHANGED``
# event. 20% is large enough that normal LMM rebalancing noise doesn't
# trigger a flood of events, but small enough that a real liquidity
# withdrawal (the load-bearing signal for the spread-capture strategy)
# surfaces inside one poll cycle.
LIQUIDITY_CHANGE_THRESHOLD = 0.20

# Maximum number of events returned by ``get_events`` when no explicit
# ``limit`` is supplied. Mirrors the W32-4 ``_MAX_GRAPH_NODES`` response-
# size hygiene convention.
DEFAULT_EVENT_LIMIT = 50

# Hard ceiling on ``get_events(limit=...)`` so a misbehaving caller
# can't OOM the bot by requesting millions of rows.
MAX_EVENT_LIMIT = 1000

# Canonical event-type vocabulary. Kept as a frozenset for the
# ``record_event`` precondition check; the DB layer accepts any TEXT
# value so a future type (e.g. ``"MARKET_RELISTED"``) doesn't require a
# migration. The set is documented so a downstream visualisation can
# switch on it without fearing churn.
EVENT_TYPES = frozenset({
    "MARKET_CREATED",
    "MARKET_SUSPENDED",
    "MARKET_REOPENED",
    "MARKET_CLOSED",
    "MARKET_RESOLVED",
    "MARKET_LIQUIDITY_CHANGED",
})

# Event types that fire an alert. ``MARKET_CREATED`` and
# ``MARKET_LIQUIDITY_CHANGED`` are too noisy for an operator alert (a
# busy day on Polymarket creates 100+ markets; liquidity fluctuates
# continuously). The four lifecycle transitions below are the high-
# signal ones — the operator wants to see them on the dashboard.
ALERT_EVENT_TYPES = frozenset({
    "MARKET_SUSPENDED",
    "MARKET_REOPENED",
    "MARKET_CLOSED",
    "MARKET_RESOLVED",
})


# ── Dataclass ──────────────────────────────────────────────────────────────────


@dataclass
class MarketEvent:
    """A single market lifecycle event.

    Attributes:
        event_id: The ingester's UUID4 for the event (the primary key on
            the ``market_events`` table). Assigned at ``record_event``
            time so the caller can use it for replay-by-id.
        event_type: One of ``EVENT_TYPES``. Documented above.
        token_id: The Polymarket market token id (the CLOB token). When
            the event is at the market level (not the token level), the
            YES token id is used (the convention every other module
            follows — ``gamma_client.extract_binary_pair`` returns
            ``(yes_token, no_token)``).
        condition_id: The Polymarket condition id (the market's logical
            identifier). Empty string when not applicable.
        slug: The market's human-readable slug. Empty string when the
            source payload doesn't supply one.
        question: The market's question text. Empty string when not
            supplied.
        timestamp: Unix epoch when the event was detected (the ingester
            assigns ``time.time()`` at the top of ``record_event``).
        payload: Free-form JSON-serialisable dict carrying the full
            source market dict at the moment of detection (so a future
            replayer can inspect the exact metadata that triggered the
            event classification). Stored as canonical JSON.
        acknowledged: ``False`` until an operator acks the event via
            ``acknowledge_event``. Mirrors the ``Alert.acknowledged``
            convention so a future dashboard can reuse the same
            ack-queue UX.
    """

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str = ""
    token_id: str = ""
    condition_id: str = ""
    slug: str = ""
    question: str = ""
    timestamp: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict)
    acknowledged: bool = False


# ── State snapshot ────────────────────────────────────────────────────────────


@dataclass
class MarketState:
    """Cached snapshot of a market's last-observed state.

    The ingester keeps one of these per token_id so the next poll can
    diff against the prior snapshot to detect transitions. Persisted to
    the ``market_state`` SQLite table so a process restart picks up
    where the prior process left off (no events missed during the
    restart window).
    """

    token_id: str
    condition_id: str = ""
    slug: str = ""
    question: str = ""
    active: bool = False
    closed: bool = False
    resolved: bool = False
    resolved_yes: bool | None = None
    liquidity: float = 0.0
    last_seen: float = 0.0


# ── Ingester ───────────────────────────────────────────────────────────────────


class MarketEventIngester:
    """Polls the Gamma API for market lifecycle events.

    Construction is import-safe — the SQLite ``_init_db`` is wrapped in
    a try/except so an unwritable default path (``/app/data`` is
    read-only in the sandbox) doesn't crash the import. On failure the
    ingester falls back to ``/tmp/market_events.db`` (mirrors the
    ``raw_vault`` / ``lineage`` fallback convention) and logs a WARNING
    so the operator sees the redirect.

    The class is safe to instantiate without env vars / paths set —
    the constructor falls back to ``DEFAULT_DB_PATH`` and creates the
    parent directory if missing. ``/app/data`` is the prod default
    (writable in the docker-compose volume); tests redirect via the
    ``MARKET_EVENTS_DB_PATH`` env var.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        gamma_client: Any | None = None,
    ) -> None:
        path_str = (
            db_path
            or os.environ.get("MARKET_EVENTS_DB_PATH", DEFAULT_DB_PATH)
        )
        self._db_path = Path(path_str)
        # ``check_same_thread=False`` so the ingester can be shared
        # between the asyncio event loop thread and ``asyncio.to_thread``
        # offloads. SQLite's default ``BEGIN IMMEDIATE`` transaction
        # gives serialisability across the GIL boundary; the ``_lock``
        # is for in-process serialisation on the (rare) multi-thread
        # call sites.
        self._lock = threading.Lock()
        # In-memory cache of the last-observed state per token_id.
        # Persisted to the ``market_state`` SQLite table so a process
        # restart picks up where the prior process left off (no events
        # missed during the restart window). Primed from the DB at
        # construction time.
        self._market_state: dict[str, MarketState] = {}
        # Counters — surfaced via ``get_stats`` for the operator
        # dashboard / observability layer.
        self._event_count: int = 0
        self._alert_count: int = 0
        self._duplicate_ignored_count: int = 0
        self._last_poll_at: float = 0.0
        self._last_poll_delta: int = 0
        # Background loop handle.
        self._running: bool = False
        self._task: asyncio.Task | None = None
        # Lazy-resolved singletons — passed in for testability.
        # Production resolves them lazily inside the methods that need
        # them so module import doesn't drag the entire ML stack into
        # memory (mirrors the ``feature_pipeline.FeaturePipeline``
        # lazy-resolution convention).
        self._gamma_client = gamma_client
        self._init_db()
        self._prime_market_state()

    # ── Schema ──────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create the ``market_events`` + ``market_state`` tables if missing."""
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Defensive: if the parent isn't writable, fall back to
            # ``/tmp/market_events.db`` so the bot still boots. A logged
            # warning surfaces the redirect; the operator can fix the
            # path env var. Mirrors the raw_vault fallback convention.
            logger.warning(
                "[market_events] Cannot create parent dir %s: %s — "
                "falling back to /tmp/market_events.db",
                self._db_path.parent,
                e,
            )
            self._db_path = Path("/tmp/market_events.db")

        try:
            with self._connect() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS market_events (
                        event_id       TEXT    PRIMARY KEY,
                        event_type     TEXT    NOT NULL,
                        token_id       TEXT    NOT NULL,
                        condition_id   TEXT    NOT NULL DEFAULT '',
                        slug           TEXT    NOT NULL DEFAULT '',
                        question       TEXT    NOT NULL DEFAULT '',
                        timestamp      REAL    NOT NULL,
                        payload        TEXT    NOT NULL,
                        acknowledged   INTEGER NOT NULL DEFAULT 0,
                        created_at     REAL    NOT NULL
                    )
                """)
                # Query indexes — mirror the ``get_events`` filter
                # dimensions so a dashboard polling by ``token_id`` /
                # ``event_type`` doesn't trigger a full table scan on
                # a large event log.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_market_events_token
                    ON market_events (token_id, timestamp DESC)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_market_events_type
                    ON market_events (event_type, timestamp DESC)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_market_events_ts
                    ON market_events (timestamp DESC)
                """)
                # Dedup UNIQUE constraint. ``INSERT OR IGNORE`` against
                # this constraint is the restart-safe dedup backstop —
                # if the ingester somehow re-emits the same event_id
                # (e.g. a partial-write recovery), the duplicate is
                # silently dropped rather than producing a duplicate
                # row. The (event_type, token_id, timestamp) triple is
                # the natural identity of a lifecycle event.
                conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_market_events_dedup
                    ON market_events (event_type, token_id, timestamp)
                """)
                # ``market_state`` — the per-token last-observed
                # snapshot. One row per token_id (UPSERTED on every
                # state change). Used to detect transitions on the next
                # poll.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS market_state (
                        token_id        TEXT    PRIMARY KEY,
                        condition_id    TEXT    NOT NULL DEFAULT '',
                        slug            TEXT    NOT NULL DEFAULT '',
                        question        TEXT    NOT NULL DEFAULT '',
                        active          INTEGER NOT NULL DEFAULT 0,
                        closed          INTEGER NOT NULL DEFAULT 0,
                        resolved        INTEGER NOT NULL DEFAULT 0,
                        resolved_yes    INTEGER,
                        liquidity       REAL    NOT NULL DEFAULT 0.0,
                        last_seen       REAL    NOT NULL
                    )
                """)
                conn.commit()
        except sqlite3.Error as e:
            logger.error("[market_events] _init_db failed: %s", e)

    def _connect(self) -> sqlite3.Connection:
        """Open a fresh connection (the ingester doesn't pool — SQLite
        file open is cheap, and a per-call connection prevents the
        ``database is locked`` errors a long-lived connection would
        surface under contention). Mirrors the ``raw_vault`` convention.
        """
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=5.0,
            isolation_level="IMMEDIATE",
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _prime_market_state(self) -> None:
        """Load every ``market_state`` row into the in-memory cache so
        a process restart doesn't lose state.

        Belt-and-braces with the on-disk table: the in-memory dict is
        the fast-path (no DB round-trip for the diff); the table is
        the restart-safe backstop. Without priming, the first poll
        after a restart would emit a ``MARKET_CREATED`` event for every
        already-known market (functionally harmless but noisy).
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM market_state"
                ).fetchall()
            for r in rows:
                self._market_state[r["token_id"]] = MarketState(
                    token_id=r["token_id"],
                    condition_id=r["condition_id"],
                    slug=r["slug"],
                    question=r["question"],
                    active=bool(r["active"]),
                    closed=bool(r["closed"]),
                    resolved=bool(r["resolved"]),
                    resolved_yes=(
                        None if r["resolved_yes"] is None
                        else bool(r["resolved_yes"])
                    ),
                    liquidity=float(r["liquidity"] or 0.0),
                    last_seen=float(r["last_seen"] or 0.0),
                )
        except sqlite3.Error as e:
            logger.warning(
                "[market_events] Failed to prime market_state from DB: %s — "
                "falling back to empty cache (first poll will emit "
                "MARKET_CREATED for every active market)",
                e,
            )

    # ── Public API ─────────────────────────────────────────────────────

    def record_event(
        self,
        event_type: str,
        token_id: str,
        condition_id: str = "",
        slug: str = "",
        question: str = "",
        payload: dict[str, Any] | None = None,
        timestamp: float | None = None,
        fire_alert: bool = True,
        wire_ml: bool = False,
    ) -> str | None:
        """Record a market lifecycle event.

        Stores the event in the ``market_events`` SQLite table, writes
        the raw payload to the W31-1 ``raw_vault`` (audit-grade
        replayability), and fires an ``alert_engine`` alert on the
        high-signal event types (``ALERT_EVENT_TYPES``). When
        ``wire_ml=True`` AND ``event_type == "MARKET_RESOLVED"``, the
        method also triggers the ML label-generation pipeline
        (``label_backfill.record_outcome`` → ``ml_model.update`` →
        ``feature_pipeline.invalidate``).

        Args:
            event_type: One of ``EVENT_TYPES`` (else ``ValueError``).
            token_id: The Polymarket market token id.
            condition_id: The market's condition id (when known).
            slug: The market's slug (when known).
            question: The market's question text (when known).
            payload: The full source market dict at the moment of
                detection. Stored as canonical JSON. ``None`` is
                treated as an empty dict.
            timestamp: When the event was detected. Defaults to
                ``time.time()``.
            fire_alert: When ``True`` (default) and ``event_type`` is
                in ``ALERT_EVENT_TYPES``, an ``alert_engine.fire_alert``
                is dispatched. Pass ``False`` to suppress (e.g. for
                test seeds).
            wire_ml: When ``True`` and ``event_type == "MARKET_RESOLVED"``,
                triggers the ML label-generation pipeline. Pass ``True``
                only when the event was detected from a real Gamma poll
                (so the payload carries the resolved outcomePrices).

        Returns:
            The ``event_id`` (UUID4 string) of the stored event, or
            ``None`` if the row was deduplicated / rejected.
        """
        if event_type not in EVENT_TYPES:
            raise ValueError(
                f"event_type must be one of {sorted(EVENT_TYPES)}, "
                f"got {event_type!r}"
            )

        evt = MarketEvent(
            event_type=event_type,
            token_id=token_id,
            condition_id=condition_id,
            slug=slug,
            question=question,
            timestamp=float(timestamp) if timestamp is not None else time.time(),
            payload=payload or {},
        )

        # Serialise the payload to canonical JSON. ``default=str`` so
        # non-JSON-serialisable objects (e.g. ``Decimal``) still
        # serialise — mirrors the ``raw_vault`` convention.
        try:
            payload_json = json.dumps(evt.payload, sort_keys=True, default=str)
        except (TypeError, ValueError) as e:
            logger.error(
                "[market_events] Cannot serialise payload for event %s "
                "token_id=%s: %s — record dropped",
                event_type, token_id, e,
            )
            with self._lock:
                self._duplicate_ignored_count += 1
            return None

        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO market_events (
                        event_id, event_type, token_id, condition_id,
                        slug, question, timestamp, payload,
                        acknowledged, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evt.event_id,
                        evt.event_type,
                        evt.token_id,
                        evt.condition_id,
                        evt.slug,
                        evt.question,
                        evt.timestamp,
                        payload_json,
                        int(evt.acknowledged),
                        time.time(),
                    ),
                )
                conn.commit()
                if cur.rowcount == 0:
                    # DB-layer dedup hit — the (event_type, token_id,
                    # timestamp) triple is already present. Mirrors the
                    # raw_vault's restart-safe backstop convention.
                    with self._lock:
                        self._duplicate_ignored_count += 1
                    return None
        except sqlite3.Error as e:
            logger.error(
                "[market_events] SQLite insert failed for event %s "
                "token_id=%s: %s",
                event_type, token_id, e,
            )
            return None

        with self._lock:
            self._event_count += 1

        # Mirror into the W31-1 raw vault (best-effort — the vault's
        # own ``record_observation`` swallows its own errors so a vault
        # hiccup can't break the event-recording path).
        try:
            from ingestion.raw_vault import raw_vault
            if raw_vault is not None:
                raw_vault.record_observation(
                    source="gamma",
                    source_id=f"market_event:{evt.event_id}",
                    event_type=f"market_{evt.event_type.lower()}",
                    raw_payload={
                        "event_id": evt.event_id,
                        "event_type": evt.event_type,
                        "token_id": evt.token_id,
                        "condition_id": evt.condition_id,
                        "slug": evt.slug,
                        "question": evt.question,
                        "timestamp": evt.timestamp,
                        "market": evt.payload,
                    },
                    event_timestamp=evt.timestamp,
                    validation_status="valid",
                    quality_score=1.0,
                )
        except Exception as e:  # noqa: BLE001 — defensive
            logger.debug(
                "[market_events] raw_vault mirror failed for event %s: %s",
                evt.event_id, e,
            )

        # Fire alert on high-signal event types.
        if fire_alert and evt.event_type in ALERT_EVENT_TYPES:
            self._fire_alert(evt)

        # Wire ML label generation on MARKET_RESOLVED.
        if wire_ml and evt.event_type == "MARKET_RESOLVED":
            try:
                resolved_yes = self._resolve_outcome(evt.payload)
                if resolved_yes is not None:
                    # The ML wiring is async (``feature_pipeline.get_features``
                    # is async) — schedule it as a fire-and-forget task so
                    # the synchronous ``record_event`` call returns
                    # immediately. If no event loop is running (e.g. a
                    # sync unit test), the wiring is silently skipped —
                    # the label is still recorded by the synchronous
                    # ``label_backfill.record_outcome`` call below.
                    self._schedule_ml_wiring(evt.token_id, resolved_yes)
            except Exception as e:  # noqa: BLE001 — defensive
                logger.debug(
                    "[market_events] ML wiring schedule failed for %s: %s",
                    evt.token_id, e,
                )

        return evt.event_id

    def get_events(
        self,
        token_id: str | None = None,
        event_type: str | None = None,
        limit: int = DEFAULT_EVENT_LIMIT,
    ) -> list[dict[str, Any]]:
        """Return events matching the filter, most-recent-first.

        Any of ``token_id`` / ``event_type`` may be ``None`` (no filter
        on that dimension). ``limit`` is clamped to ``[1, MAX_EVENT_LIMIT]``
        so a misbehaving caller can't OOM the bot.
        """
        cap = max(1, min(int(limit), MAX_EVENT_LIMIT))
        sql = "SELECT * FROM market_events"
        clauses: list[str] = []
        params: list[Any] = []
        if token_id is not None:
            clauses.append("token_id = ?")
            params.append(token_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(cap)
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            logger.error("[market_events] get_events failed: %s", e)
            return []
        return [_row_to_event_dict(r) for r in rows]

    def acknowledge_event(self, event_id: str) -> bool:
        """Mark an event as acknowledged. Returns ``True`` if a row was
        updated, ``False`` if the event_id wasn't found.
        """
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE market_events SET acknowledged = 1 "
                    "WHERE event_id = ?",
                    (event_id,),
                )
                conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error("[market_events] acknowledge_event failed: %s", e)
            return False

    # ── Detection (the polling entry point) ─────────────────────────────

    async def detect_events(self) -> int:
        """Poll the Gamma API and emit events for every detected
        transition.

        Returns the number of events emitted. Safe to call directly
        (e.g. from a CLI or test) — it does not depend on the background
        loop running.

        The detection order is intentional:
          1. Active markets — emit ``MARKET_CREATED`` for new tokens,
             ``MARKET_SUSPENDED`` / ``MARKET_REOPENED`` for active-state
             transitions, ``MARKET_LIQUIDITY_CHANGED`` for big deltas.
          2. Resolved markets — emit ``MARKET_CLOSED`` /
             ``MARKET_RESOLVED`` for newly closed / resolved markets.
        """
        gamma = self._resolve_gamma_client()
        if gamma is None:
            logger.warning(
                "[market_events] gamma_client unavailable — skipping poll"
            )
            return 0

        emitted = 0
        try:
            active_markets = await gamma.get_markets(
                active=True, closed=False, limit=100,
                order="volume24hr", ascending=False,
            )
        except Exception as e:
            logger.warning(
                "[market_events] Gamma active-markets fetch failed: %s", e
            )
            active_markets = []

        try:
            resolved_markets = await gamma.get_resolved_markets(limit=100)
        except Exception as e:
            logger.warning(
                "[market_events] Gamma resolved-markets fetch failed: %s", e
            )
            resolved_markets = []

        # Active markets → creations / suspensions / reopens / liquidity.
        for mkt in active_markets or []:
            try:
                emitted += self._detect_active_transitions(mkt)
            except Exception as e:  # noqa: BLE001 — defensive
                logger.debug(
                    "[market_events] active-transition detection error: %s", e
                )

        # Resolved markets → closures / resolutions.
        for mkt in resolved_markets or []:
            try:
                emitted += self._detect_resolution_transitions(mkt)
            except Exception as e:  # noqa: BLE001 — defensive
                logger.debug(
                    "[market_events] resolution-transition detection error: %s",
                    e,
                )

        with self._lock:
            self._last_poll_at = time.time()
            self._last_poll_delta = emitted
        return emitted

    def _detect_active_transitions(self, market: dict) -> int:
        """Detect ``MARKET_CREATED`` / ``MARKET_SUSPENDED`` /
        ``MARKET_REOPENED`` / ``MARKET_LIQUIDITY_CHANGED`` for one
        active market.

        Returns the number of events emitted for this market (0 or 1
        per call — at most one transition fires per poll per market so
        the event log stays readable).
        """
        token_ids = self._extract_token_ids(market)
        if not token_ids:
            return 0
        # Use the YES token (index 0) as the canonical identifier —
        # mirrors the convention in ``gamma_client.extract_binary_pair``.
        token_id = str(token_ids[0])
        condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
        slug = str(market.get("slug") or "")
        question = str(market.get("question") or market.get("title") or "")
        active = bool(market.get("active", True))
        closed = bool(market.get("closed", False))
        try:
            liquidity = float(market.get("liquidity") or 0.0)
        except (TypeError, ValueError):
            liquidity = 0.0

        prior = self._market_state.get(token_id)
        emitted = 0

        if prior is None:
            # New market — fire MARKET_CREATED.
            eid = self.record_event(
                event_type="MARKET_CREATED",
                token_id=token_id,
                condition_id=condition_id,
                slug=slug,
                question=question,
                payload=market,
                fire_alert=False,  # too noisy for an operator alert
            )
            if eid is not None:
                emitted += 1
        else:
            # Active-state transition?
            if prior.active and not active:
                eid = self.record_event(
                    event_type="MARKET_SUSPENDED",
                    token_id=token_id, condition_id=condition_id,
                    slug=slug, question=question, payload=market,
                )
                if eid is not None:
                    emitted += 1
            elif (not prior.active) and active and (not prior.resolved):
                eid = self.record_event(
                    event_type="MARKET_REOPENED",
                    token_id=token_id, condition_id=condition_id,
                    slug=slug, question=question, payload=market,
                )
                if eid is not None:
                    emitted += 1

            # Liquidity delta?
            if prior.liquidity > 0.0:
                try:
                    delta = abs(liquidity - prior.liquidity) / prior.liquidity
                except ZeroDivisionError:
                    delta = 0.0
                if delta >= LIQUIDITY_CHANGE_THRESHOLD:
                    eid = self.record_event(
                        event_type="MARKET_LIQUIDITY_CHANGED",
                        token_id=token_id, condition_id=condition_id,
                        slug=slug, question=question,
                        payload={
                            "prior_liquidity": prior.liquidity,
                            "new_liquidity": liquidity,
                            "delta_pct": round(delta, 4),
                            "market": market,
                        },
                        fire_alert=False,  # too noisy for an operator alert
                    )
                    if eid is not None:
                        emitted += 1

        # Update the in-memory + on-disk state snapshot.
        new_state = MarketState(
            token_id=token_id,
            condition_id=condition_id,
            slug=slug,
            question=question,
            active=active,
            closed=closed,
            resolved=False,
            resolved_yes=None,
            liquidity=liquidity,
            last_seen=time.time(),
        )
        self._upsert_state(new_state)
        return emitted

    def _detect_resolution_transitions(self, market: dict) -> int:
        """Detect ``MARKET_CLOSED`` / ``MARKET_RESOLVED`` for one
        resolved market.

        Returns the number of events emitted for this market (0 or 1
        per call — at most one transition fires per poll per market).
        """
        token_ids = self._extract_token_ids(market)
        if not token_ids:
            return 0
        token_id = str(token_ids[0])
        condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
        slug = str(market.get("slug") or "")
        question = str(market.get("question") or market.get("title") or "")
        closed = bool(market.get("closed", True))
        resolved_yes = self._resolve_outcome(market)

        prior = self._market_state.get(token_id)
        emitted = 0

        # ``MARKET_CLOSED`` — first time we see closed=True on a market
        # that was previously active (or unseen). Suppresses when the
        # market is already resolved (a resolution implies closure, so
        # we don't double-fire).
        if closed and (prior is None or (prior.active and not prior.closed and not prior.resolved)):
            eid = self.record_event(
                event_type="MARKET_CLOSED",
                token_id=token_id, condition_id=condition_id,
                slug=slug, question=question, payload=market,
            )
            if eid is not None:
                emitted += 1

        # ``MARKET_RESOLVED`` — first time we see a parseable YES/NO
        # outcome on a market that wasn't already resolved.
        if resolved_yes is not None and (prior is None or not prior.resolved):
            eid = self.record_event(
                event_type="MARKET_RESOLVED",
                token_id=token_id, condition_id=condition_id,
                slug=slug, question=question,
                payload={
                    "resolved_yes": resolved_yes,
                    "outcome_prices": market.get("outcomePrices"),
                    "market": market,
                },
                wire_ml=True,
            )
            if eid is not None:
                emitted += 1

        # Update the in-memory + on-disk state snapshot.
        new_state = MarketState(
            token_id=token_id,
            condition_id=condition_id,
            slug=slug,
            question=question,
            active=False,
            closed=closed,
            resolved=resolved_yes is not None,
            resolved_yes=resolved_yes,
            liquidity=0.0,
            last_seen=time.time(),
        )
        self._upsert_state(new_state)
        return emitted

    # ── ML label generation wiring (Step 3) ─────────────────────────────

    def _schedule_ml_wiring(self, token_id: str, resolved_yes: bool) -> None:
        """Schedule the ML label-generation wiring as a fire-and-forget
        asyncio task.

        The wiring is async (``feature_pipeline.get_features`` is
        async). If no event loop is running (sync test), the wiring is
        silently skipped — the synchronous ``label_backfill.record_outcome``
        call still happens, but the online ``ml_model.update`` /
        ``feature_pipeline.invalidate`` calls are deferred to the next
        loop tick (or skipped entirely if no loop ever runs).
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._wire_ml_label_generation(token_id, resolved_yes))
        except RuntimeError:
            # No running loop — call the sync path eagerly so the label
            # is at least recorded (the online update + cache invalidation
            # are deferred to the next loop tick).
            self._wire_ml_label_generation_sync(token_id, resolved_yes)

    async def _wire_ml_label_generation(
        self, token_id: str, resolved_yes: bool
    ) -> None:
        """Async ML label-generation wiring (called as a task).

        Order matters:
          1. ``label_backfill.record_outcome(token_id, outcome)`` —
             persist the resolved label so the daily backfill loop can
             retrain on it.
          2. ``feature_pipeline.get_features(token_id)`` →
             ``ml_model.update(features, outcome_yes)`` — online SGD
             update so the model learns from the resolution
             immediately (without waiting for the daily retrain).
          3. ``feature_pipeline.invalidate(token_id)`` — clear the
             cached price-history deque so the NEXT prediction uses
             fresh data (the resolved market's old mid no longer
             reflects reality).
        """
        outcome = 1 if resolved_yes else 0

        # 1. Record the resolved label (sync — wraps timescale_db).
        try:
            from core.label_backfill import label_backfill_engine
            label_backfill_engine.record_outcome(token_id, outcome)
        except Exception as e:  # noqa: BLE001 — defensive
            logger.debug(
                "[market_events] label_backfill.record_outcome failed for "
                "%s: %s", token_id, e,
            )

        # 2. Online ML update — fetch features, then call ml_model.update.
        features = None
        try:
            from ingestion.feature_pipeline import get_feature_pipeline
            pipe = get_feature_pipeline()
            features = await pipe.get_features(token_id)
        except Exception as e:  # noqa: BLE001 — defensive
            logger.debug(
                "[market_events] feature_pipeline.get_features failed for "
                "%s: %s", token_id, e,
            )
        if features is not None:
            try:
                from ml.model import ml_model
                await asyncio.to_thread(ml_model.update, features, resolved_yes)
            except Exception as e:  # noqa: BLE001 — defensive
                logger.debug(
                    "[market_events] ml_model.update failed for %s: %s",
                    token_id, e,
                )

        # 3. Invalidate cached features.
        try:
            from ingestion.feature_pipeline import get_feature_pipeline
            pipe = get_feature_pipeline()
            pipe.invalidate(token_id)
        except Exception as e:  # noqa: BLE001 — defensive
            logger.debug(
                "[market_events] feature_pipeline.invalidate failed for "
                "%s: %s", token_id, e,
            )

    def _wire_ml_label_generation_sync(
        self, token_id: str, resolved_yes: bool
    ) -> None:
        """Sync fallback when no event loop is running.

        Only the label-recording step runs (the online update + cache
        invalidation are async-only). The label is still durably
        recorded so the daily backfill retrain will pick it up.
        """
        outcome = 1 if resolved_yes else 0
        try:
            from core.label_backfill import label_backfill_engine
            label_backfill_engine.record_outcome(token_id, outcome)
        except Exception as e:  # noqa: BLE001 — defensive
            logger.debug(
                "[market_events] (sync) label_backfill.record_outcome failed "
                "for %s: %s", token_id, e,
            )

    # ── Alerting ───────────────────────────────────────────────────────

    def _fire_alert(self, event: MarketEvent) -> None:
        """Fire a ``core.alerting`` alert for the high-signal event."""
        try:
            from core.alerting import Alert, alert_engine
            severity = (
                "critical" if event.event_type == "MARKET_RESOLVED"
                else "warning"
            )
            alert = Alert(
                alert_id=f"market_event:{event.event_id}",
                timestamp=event.timestamp,
                category="data",
                name=f"market_{event.event_type.lower()}",
                severity=severity,
                message=(
                    f"{event.event_type} — {event.question or event.slug or event.token_id[:12]}"
                ),
                value=None,
                threshold=None,
                metadata={
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "token_id": event.token_id,
                    "condition_id": event.condition_id,
                    "slug": event.slug,
                    "question": event.question,
                },
                acknowledged=False,
            )
            alert_engine.fire_alert(alert)
            with self._lock:
                self._alert_count += 1
        except Exception as e:  # noqa: BLE001 — defensive
            logger.debug(
                "[market_events] alert fire failed for event %s: %s",
                event.event_id, e,
            )

    # ── State persistence ──────────────────────────────────────────────

    def _upsert_state(self, state: MarketState) -> None:
        """Upsert the market-state snapshot into the in-memory cache +
        the ``market_state`` SQLite table.

        Best-effort — a SQLite write failure is logged + swallowed so
        the event-recording path never breaks because of a state-cache
        hiccup (mirrors the raw_vault best-effort contract).
        """
        with self._lock:
            self._market_state[state.token_id] = state
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO market_state (
                        token_id, condition_id, slug, question,
                        active, closed, resolved, resolved_yes,
                        liquidity, last_seen
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        state.token_id,
                        state.condition_id,
                        state.slug,
                        state.question,
                        int(state.active),
                        int(state.closed),
                        int(state.resolved),
                        None if state.resolved_yes is None else int(state.resolved_yes),
                        state.liquidity,
                        state.last_seen,
                    ),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.debug(
                "[market_events] market_state upsert failed for %s: %s",
                state.token_id, e,
            )

    # ── Background loop ────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background polling loop (idempotent)."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._loop(), name="market-event-ingester"
        )
        logger.info(
            "[market_events] Started — poll_interval=%.0fs, "
            "liquidity_threshold=%.0f%%",
            POLL_INTERVAL_SECONDS, LIQUIDITY_CHANGE_THRESHOLD * 100,
        )

    async def stop(self) -> None:
        """Cancel the background polling task and await clean shutdown."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001 — defensive
                logger.debug("[market_events] Task teardown raised: %s", e)
            self._task = None

    async def _loop(self) -> None:
        """Background polling loop — calls ``detect_events`` every
        ``POLL_INTERVAL_SECONDS``.
        """
        # Brief warm-up so the API server binds immediately.
        try:
            await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            return
        while self._running:
            try:
                emitted = await self.detect_events()
                if emitted > 0:
                    logger.info(
                        "[market_events] Poll emitted %d events", emitted
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — never tear down the loop
                logger.warning(
                    "[market_events] Poll failed: %s", e, exc_info=True
                )
            try:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return

    # ── Stats / observability ──────────────────────────────────────────

    @property
    def stats(self) -> dict[str, Any]:
        """Return a snapshot of the ingester's lifetime telemetry."""
        with self._lock:
            return {
                "running": self._running,
                "event_count": self._event_count,
                "alert_count": self._alert_count,
                "duplicate_ignored_count": self._duplicate_ignored_count,
                "tracked_markets": len(self._market_state),
                "last_poll_at": self._last_poll_at,
                "last_poll_delta": self._last_poll_delta,
                "poll_interval_seconds": POLL_INTERVAL_SECONDS,
                "liquidity_change_threshold": LIQUIDITY_CHANGE_THRESHOLD,
                "db_path": str(self._db_path),
            }

    def reset_stats(self) -> None:
        """Zero the in-memory counters (test-only — does NOT truncate
        the DB). Mirrors the ``raw_vault.reset_stats`` convention.
        """
        with self._lock:
            self._event_count = 0
            self._alert_count = 0
            self._duplicate_ignored_count = 0
            self._last_poll_at = 0.0
            self._last_poll_delta = 0
            self._market_state.clear()

    def truncate(self) -> None:
        """Truncate BOTH the in-memory cache AND the on-disk SQLite
        tables (``market_events`` + ``market_state``).

        Test-only — production NEVER calls this (every event survives
        for audit). Used by ``tests/test_market_events.py``'s autouse
        ``_reset_market_event_ingester`` fixture so each test starts
        from a clean state without cross-test pollution. Mirrors the
        ``dead_letter_queue.clear()`` convention.
        """
        with self._lock:
            self._event_count = 0
            self._alert_count = 0
            self._duplicate_ignored_count = 0
            self._last_poll_at = 0.0
            self._last_poll_delta = 0
            self._market_state.clear()
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM market_events")
                conn.execute("DELETE FROM market_state")
                conn.commit()
        except sqlite3.Error as e:
            logger.debug(
                "[market_events] truncate failed: %s", e
            )

    # ── Lazy singleton resolution ──────────────────────────────────────

    def _resolve_gamma_client(self) -> Any | None:
        """Return the configured gamma_client, or the module-level
        singleton when none was injected.
        """
        if self._gamma_client is not None:
            return self._gamma_client
        try:
            from core.gamma_client import gamma_client as _g
            self._gamma_client = _g
            return _g
        except Exception as e:  # noqa: BLE001 — defensive
            logger.warning(
                "[market_events] gamma_client unavailable: %s", e
            )
            return None

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _extract_token_ids(market: dict) -> list[str]:
        """Extract token ids from a Gamma market dict.

        Delegates to ``GammaClient.extract_token_ids`` so the parsing
        logic stays in one place (the W13-2 ``gamma_client`` module
        handles every shape Polymarket throws at us — ``tokens`` list,
        ``clobTokenIds`` JSON string, ``clobTokenIds`` list).
        """
        try:
            from core.gamma_client import GammaClient
            return GammaClient.extract_token_ids(market)
        except Exception as e:  # noqa: BLE001 — defensive
            logger.debug(
                "[market_events] extract_token_ids failed: %s", e
            )
            return []

    @staticmethod
    def _resolve_outcome(market: dict | None) -> bool | None:
        """Parse ``outcomePrices`` to determine if the YES outcome won.

        Returns ``True`` if YES won, ``False`` if NO won, ``None`` if
        unresolvable. Mirrors the convention used by
        ``core.label_backfill.LabelBackfillEngine._resolve_outcome`` and
        ``core.settlement`` (YES price ≥ 0.9 = winner).
        """
        if not market or not isinstance(market, dict):
            return None
        outcome_prices = market.get("outcomePrices")
        if not outcome_prices:
            return None
        if isinstance(outcome_prices, str):
            try:
                prices = json.loads(outcome_prices)
            except Exception:
                return None
        else:
            prices = outcome_prices
        if not prices or len(prices) < 2:
            return None
        try:
            p0 = float(prices[0])
        except (TypeError, ValueError):
            return None
        return p0 >= 0.9


# ── Module-level helpers ──────────────────────────────────────────────────────


def _row_to_event_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a ``market_events`` row to a dict with ``payload``
    parsed back from JSON.
    """
    d = dict(row)
    raw = d.get("payload")
    if isinstance(raw, str):
        try:
            d["payload"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    # Normalise the SQLite integer to a Python bool for the
    # ``acknowledged`` field so the API response is JSON-friendly.
    if "acknowledged" in d:
        d["acknowledged"] = bool(d["acknowledged"])
    return d


# ── Module-level singleton ────────────────────────────────────────────────────
# Mirrors the convention used by every sibling ingestion module
# (``ingestion.raw_vault.raw_vault``, ``ingestion.lineage.lineage_tracker`` …).
# Importers grab it at module-import time; the constructor opens the DB,
# runs migrations, and primes the state cache — the I/O is bounded (a
# single SQLite file + one ``SELECT * FROM market_state`` query) so the
# import-time cost is negligible.
market_event_ingester = MarketEventIngester()


__all__ = [
    "MarketEvent",
    "MarketState",
    "MarketEventIngester",
    "market_event_ingester",
    "EVENT_TYPES",
    "ALERT_EVENT_TYPES",
    "DEFAULT_DB_PATH",
    "POLL_INTERVAL_SECONDS",
    "LIQUIDITY_CHANGE_THRESHOLD",
    "DEFAULT_EVENT_LIMIT",
    "MAX_EVENT_LIMIT",
]
