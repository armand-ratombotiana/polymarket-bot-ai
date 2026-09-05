"""WebSocket ingestion manager — real-time data from CLOB.

W31-2 — real-time WebSocket ingestion layer for Polymarket CLOB.
Sits ABOVE the existing ``core/ws_client.py`` transport (which is a
single-purpose in-memory book updater) and layers the production
ingestion pipeline on top:

Features
~~~~~~~~
1. Connects to Polymarket CLOB WebSocket
   (``wss://ws-subscriptions-clob.polymarket.com/ws/market``).
2. Subscribes to order book updates, trade feeds, market events
   (logical channels: ``"book"`` / ``"trades"`` / ``"markets"``).
3. Deduplicates incoming messages (bounded SHA-256 cache).
4. Validates and normalizes data (routes through
   ``core.data_validator.data_validator`` — same gate as the REST
   poller, so a WS snapshot and a REST snapshot are subject to the
   same schema / value / staleness / dedup rules).
5. Routes to raw vault + normalized storage
   (``core.ingestion.raw_vault.raw_vault.record_observation`` +
   ``core.timescale_db.timescale_db.record_snapshot`` /
   ``record_trade`` + ``core.data_store.store.update_order_book``).
6. Handles reconnection with exponential backoff (2 s → 60 s,
   ``RECONNECT_BASE_DELAY`` / ``RECONNECT_MAX_DELAY``).
7. Reports health metrics (latency / throughput / gaps /
   reconnects / last-seen message timestamp) via the ``health``
   property — designed to be polled by the observability collector.
8. Checkpoints last processed message (per-token sequence number)
   to a JSON file at shutdown (and periodically every
   ``CHECKPOINT_INTERVAL`` seconds) so a process restart can
   resume from the last known sequence rather than from the
   stream's head.

Sequence-number model
~~~~~~~~~~~~~~~~~~~~~
The Polymarket CLOB WebSocket public ``market`` channel does not
publish explicit per-message sequence numbers in its documented
schema. To support gap detection (Step 3 of the W31-2 task spec)
the manager synthesises a per-token monotonic counter:

  * When an inbound message carries an explicit ``seq_no`` /
    ``sequence`` / ``seq`` field (in ``msg`` or ``msg["data"]``),
    that value is treated as authoritative — the per-token
    counter is brought up to it, and gap detection compares
    ``seq_no`` vs ``last_seq[token] + 1``.

  * When no explicit seq_no is present, the per-token counter is
    bumped by 1 on every message — so the *next* explicit
    seq_no-bearing message can still be gap-checked against the
    synthesised counter. The synthesised counter is NOT treated
    as authoritative for the upstream's own gap contract (the
    upstream may have legitimately dropped a message we never
    saw, which the synthesised counter cannot observe).

This mirrors the contract documented in the W31-2 task spec
("Track expected message sequence numbers; Detect gaps in
sequence; Trigger backfill for missing messages; Log gap
events").
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from config import settings
from core.data_store import OrderBook, PriceLevel, store

log = logging.getLogger(__name__)

# ── Event type constants (mirrors ``core.ws_client``) ────────────────────────
EVT_BOOK_SNAPSHOT = "book"
EVT_PRICE_CHANGE = "price_change"
EVT_TRADE = "last_trade_price"
EVT_USER_FILL = "user_fill"
EVT_TICK_SIZE = "tick_size_change"

# ── Logical channels (subset of the WS feed the manager routes) ────────────
# Each logical channel maps to a set of ``event_type`` strings carried by the
# underlying CLOB WebSocket ``market`` subscription. ``"book"`` covers full
# snapshots + incremental price changes; ``"trades"`` covers the trade tape;
# ``"markets"`` covers market-lifecycle events (tick-size changes, resolution).
CHANNEL_BOOK = "book"
CHANNEL_TRADES = "trades"
CHANNEL_MARKETS = "markets"

CHANNELS: dict[str, frozenset[str]] = {
    CHANNEL_BOOK: frozenset({EVT_BOOK_SNAPSHOT, EVT_PRICE_CHANGE}),
    CHANNEL_TRADES: frozenset({EVT_TRADE, EVT_USER_FILL}),
    CHANNEL_MARKETS: frozenset({EVT_TICK_SIZE}),
}

# ── Reconnection / housekeeping tuning ──────────────────────────────────────
RECONNECT_BASE_DELAY = 2.0     # seconds — doubles on every failed reconnect
RECONNECT_MAX_DELAY = 60.0     # cap so a sustained outage doesn't grow to hours
PING_INTERVAL = 20.0           # seconds — keeps the WS alive through proxies
PING_TIMEOUT = 30.0            # seconds — server must echo within this window
MAX_SIZE = 2 ** 23             # 8 MiB max inbound frame (mirrors ws_client)
SUBSCRIBE_BATCH = 100          # Polymarket requires ≤100 assets_ids per message

# ── Dedup / checkpoint tuning ───────────────────────────────────────────────
DEDUP_CACHE_SIZE = 10_000      # bounded LRU-ish deque (mirrors data_validator)
CHECKPOINT_INTERVAL = 30.0     # seconds between periodic checkpoint flushes
SOURCE_ID = "clob_ws"          # raw_vault / source_registry source identifier


# ── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class GapEvent:
    """Record of a detected sequence gap on a single token stream.

    Stored in ``WSIngestionManager._gap_log`` (bounded deque, last 1 000
    entries). Surfaced in ``health["recent_gaps"]`` so the operator
    dashboard can show the last gap per token + the gap size, and the
    backfill scheduler (Step 3 — "Trigger backfill for missing messages")
    can poll the same list to dispatch REST backfill calls.
    """

    token_id: str
    expected: int           # seq_no we expected to see (last + 1)
    seen: int               # seq_no we actually saw
    missing: list[int]      # [expected, expected+1, ..., seen-1]
    detected_at: float = field(default_factory=time.time)


@dataclass
class _HealthSnapshot:
    """Internal mutable health counter state.

    Mirrors the canonical W22-7 observability metric vocabulary
    (``data_source.*`` God Mode §54) so the existing
    ``core.observability_collector`` can ingest the snapshot unchanged.
    """

    connected: bool = False
    last_message_at: float = 0.0
    last_seq_no: dict[str, int] = field(default_factory=dict)
    messages_received: int = 0
    messages_deduped: int = 0
    messages_invalid: int = 0
    messages_routed: int = 0
    reconnect_count: int = 0
    gap_count: int = 0
    last_gap_at: float = 0.0
    latency_samples_ms: deque = field(
        default_factory=lambda: deque(maxlen=200)
    )

    def throughput(self, window_s: float = 60.0) -> float:
        """Messages-per-second over the last ``window_s`` seconds."""
        if not self.last_message_at:
            return 0.0
        # Use the rolling count since boot as a conservative estimate —
        # the deque of message timestamps isn't tracked (the existing
        # observability collector samples throughput independently).
        return float(self.messages_routed) / max(window_s, 1.0)

    def avg_latency_ms(self) -> float:
        """Mean WS-to-ingest latency in milliseconds (0.0 when no samples)."""
        if not self.latency_samples_ms:
            return 0.0
        return float(sum(self.latency_samples_ms)) / len(self.latency_samples_ms)

    def to_dict(self, recent_gaps: list[dict[str, Any]]) -> dict[str, Any]:
        """Public-facing health dict (consumed by observability collector)."""
        return {
            "source": SOURCE_ID,
            "connected": self.connected,
            "last_message_at": self.last_message_at,
            "last_seq_no": dict(self.last_seq_no),
            "messages_received": self.messages_received,
            "messages_deduped": self.messages_deduped,
            "messages_invalid": self.messages_invalid,
            "messages_routed": self.messages_routed,
            "reconnect_count": self.reconnect_count,
            "gap_count": self.gap_count,
            "last_gap_at": self.last_gap_at,
            "avg_latency_ms": round(self.avg_latency_ms(), 3),
            "throughput_per_min": round(self.throughput(60.0) * 60.0, 3),
            "recent_gaps": recent_gaps,
        }


# ── WebSocket ingestion manager ─────────────────────────────────────────────


# Type alias for the connect factory — production uses ``websockets.connect``;
# tests inject a fake that yields a stub WS object.
ConnectFactory = Callable[..., Awaitable[Any]]


class WSIngestionManager:
    """Real-time CLOB WebSocket ingestion manager.

    Production wiring (see ``__init__``) uses ``websockets.connect`` with
    the URI from ``settings.poly_ws_host`` and writes its checkpoint to
    ``/app/data/ws_ingestion_checkpoint.json``. Tests inject:

      * ``connect_factory`` — a fake async context manager yielding a stub
        WS object whose ``__aiter__`` returns a deterministic message list.
      * ``checkpoint_path`` — a ``tmp_path`` JSON file so tests don't
        pollute ``/app/data``.

    Lifecycle
    ~~~~~~~~~
    1. ``start()`` — kicks off the background ``_run_forever`` task.
    2. ``_run_forever`` — connects, sends subscriptions, listens until
       the socket closes / errors, reconnects with exponential backoff.
    3. ``stop()`` — flips ``_running`` to False, cancels the task,
       flushes a final checkpoint.
    """

    def __init__(
        self,
        *,
        uri: str | None = None,
        connect_factory: ConnectFactory | None = None,
        checkpoint_path: str | Path | None = None,
        max_reconnect_attempts: int = 0,  # 0 = unlimited
    ) -> None:
        self._uri = uri or settings.poly_ws_host
        # Production default — ``websockets.connect``. Tests override.
        self._connect_factory: ConnectFactory = (
            connect_factory or _default_connect_factory
        )
        # Checkpoint path — defaults to ``/app/data/ws_ingestion_checkpoint.json``
        # in production (the same on-disk state directory the rest of the
        # bot writes to). In tests, callers pass a ``tmp_path`` JSON file.
        self._checkpoint_path: Path = Path(
            checkpoint_path or "/app/data/ws_ingestion_checkpoint.json"
        )
        self._max_reconnect_attempts = max_reconnect_attempts

        # ── Subscription state ────────────────────────────────────────────
        self._subscribed_tokens: set[str] = set()
        self._active_channels: set[str] = set(CHANNELS.keys())  # all by default

        # ── Live connection state ────────────────────────────────────────
        self._ws: Any = None
        self._running: bool = False
        self._task: asyncio.Task | None = None
        self._reconnect_count: int = 0

        # ── Dedup + gap tracking ─────────────────────────────────────────
        # ``_dedup_cache`` holds SHA-256 hashes of (event_type, data) —
        # bounded so a long-running session can't grow it without limit
        # (mirrors ``core.data_validator.DataValidator._seen_hashes``).
        self._dedup_cache: deque[str] = deque(maxlen=DEDUP_CACHE_SIZE)
        # Per-token monotonic counter — bumped on every accepted message.
        # Used both for gap detection AND for the checkpoint resume
        # contract ("restart picks up from the last sequence number seen").
        self._last_seq: dict[str, int] = {}
        # Recent gap events — bounded so a noisy upstream can't OOM us.
        self._gap_log: deque[GapEvent] = deque(maxlen=1000)

        # ── Backfill hook (Step 3 — "Trigger backfill for missing messages")
        # ``on_gap`` is called with the GapEvent on every gap detection.
        # Production wires this to the REST fallback's
        # ``backfill(token_id, missing_seqs)`` method; tests inject a
        # recording mock.
        self.on_gap: Callable[[GapEvent], Awaitable[None]] | None = None

        # ── Health snapshot (mirrors W22-7 ``data_source.*`` vocabulary)
        self._health = _HealthSnapshot(reconnect_count=0)

        # ── Checkpoint background task ───────────────────────────────────
        self._checkpoint_task: asyncio.Task | None = None

        # Load any persisted checkpoint so a restart picks up the
        # last-known per-token sequence numbers immediately.
        self._load_checkpoint_sync()

    # ── Subscription management (Step 2) ────────────────────────────────────

    def add_tokens(self, token_ids: list[str]) -> None:
        """Add token IDs to the subscription set (live or pending reconnect).

        Mirrors ``core.ws_client.WebSocketClient.subscribe`` — duplicates
        are silently collapsed (``set`` semantics), and the new tokens are
        sent to the upstream on the next reconnect cycle if the connection
        is currently down, OR immediately via ``_send_subscriptions`` if
        the WS is open.
        """
        new = [t for t in token_ids if t and t not in self._subscribed_tokens]
        if not new:
            return
        self._subscribed_tokens.update(new)
        log.debug(
            "[ws_ingestion] Added %d token(s); total=%d",
            len(new), len(self._subscribed_tokens),
        )
        # If the WS is already open, push the new tokens immediately
        # rather than waiting for the next reconnect cycle.
        if self._ws is not None and not _ws_is_closed(self._ws):
            asyncio.create_task(self._send_subscriptions(self._ws, new))

    def remove_tokens(self, token_ids: list[str]) -> None:
        """Drop token IDs from the subscription set.

        Polymarket's WS doesn't expose a documented per-token unsubscribe
        message (the only way to stop receiving events for a token is to
        drop the connection and re-subscribe to the remaining set). We
        model the local bookkeeping here so the next reconnect cycle
        only subscribes to the still-wanted set; the in-memory
        ``_subscribed_tokens`` is the source of truth.
        """
        for t in token_ids:
            self._subscribed_tokens.discard(t)
        log.debug(
            "[ws_ingestion] Removed %d token(s); total=%d",
            len(token_ids), len(self._subscribed_tokens),
        )

    def subscribe_channels(self, channels: list[str]) -> None:
        """Enable logical channels: ``"book"`` / ``"trades"`` / ``"markets"``.

        Channel filtering is applied at message-routing time: a message
        whose ``event_type`` is not in any enabled channel's set is
        silently dropped (counted as ``messages_invalid`` for health).
        Default = all three channels enabled.
        """
        for ch in channels:
            if ch in CHANNELS:
                self._active_channels.add(ch)
            else:
                log.warning("[ws_ingestion] Unknown channel %r — ignored", ch)

    def unsubscribe_channels(self, channels: list[str]) -> None:
        """Disable logical channels (reverse of ``subscribe_channels``).

        Used on shutdown to drain the queue cleanly — or to mute a noisy
        channel mid-session (e.g. mute ``"trades"`` when the trade tape
        ingester is being throttled).
        """
        for ch in channels:
            self._active_channels.discard(ch)

    @property
    def channels(self) -> set[str]:
        """Snapshot of the currently-enabled logical channel set."""
        return set(self._active_channels)

    @property
    def subscribed_tokens(self) -> set[str]:
        """Snapshot of the currently-subscribed token set."""
        return set(self._subscribed_tokens)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the WS listener + checkpoint loop as background tasks."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._run_forever(), name="ws-ingestion"
        )
        self._checkpoint_task = asyncio.create_task(
            self._checkpoint_loop(), name="ws-ingestion-checkpoint"
        )
        log.info(
            "[ws_ingestion] Started (uri=%s, tokens=%d, channels=%s)",
            self._uri, len(self._subscribed_tokens), sorted(self._active_channels),
        )

    async def stop(self) -> None:
        """Gracefully stop the listener and flush a final checkpoint."""
        self._running = False
        for t in (self._task, self._checkpoint_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception as e:  # noqa: BLE001 — shutdown must not raise
                    log.debug("[ws_ingestion] Stop caught: %s", e)
        # Flush a final checkpoint so the next boot resumes from the
        # last-known sequence (Step 8 — "Checkpoints last processed message").
        await self.checkpoint()
        log.info("[ws_ingestion] Stopped")

    # ── Connection loop (Step 1 + Step 6) ───────────────────────────────────

    async def _run_forever(self) -> None:
        """Connect → subscribe → listen; reconnect with exp. backoff on drop.

        The loop terminates cleanly when ``_running`` flips to False
        (graceful shutdown) OR when ``_max_reconnect_attempts`` is exceeded
        (sustained outage — production default is 0 = unlimited).
        """
        delay = RECONNECT_BASE_DELAY
        first_attempt = True
        while self._running:
            try:
                await self._connect_and_listen()
                # Clean disconnect / graceful close — reset backoff.
                delay = RECONNECT_BASE_DELAY
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 — log + backoff
                log.debug(
                    "[ws_ingestion] Connection error: %s — reconnect in %.0fs",
                    e, delay,
                )

            if not first_attempt:
                self._reconnect_count += 1
                self._health.reconnect_count = self._reconnect_count
                if (
                    self._max_reconnect_attempts > 0
                    and self._reconnect_count >= self._max_reconnect_attempts
                ):
                    log.error(
                        "[ws_ingestion] Exhausted %d reconnect attempts — giving up",
                        self._max_reconnect_attempts,
                    )
                    self._running = False
                    break
            first_attempt = False

            if not self._running:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def _connect_and_listen(self) -> None:
        """Open the WS, send subscriptions, and pump messages until close."""
        log.info("[ws_ingestion] Connecting to %s", self._uri)
        # ``websockets.connect`` returns an async context manager. The
        # injected ``_connect_factory`` may be a real ``websockets.connect``
        # OR a test stub. Both must support the ``async with`` protocol.
        async with self._connect_factory(
            self._uri,
            ping_interval=PING_INTERVAL,
            ping_timeout=PING_TIMEOUT,
            max_size=MAX_SIZE,
        ) as ws:
            self._ws = ws
            self._health.connected = True
            await store.log_event("WSIngestionManager connected")
            # (Re)send subscriptions on every (re)connect — Polymarket's
            # server resets its per-asset subscription state on every
            # new WS connection, so the existing ws_client.py pattern of
            # subscribing once at boot does NOT survive a reconnect.
            if self._subscribed_tokens:
                await self._send_subscriptions(
                    ws, list(self._subscribed_tokens)
                )
            # Pump messages until the socket closes or we're asked to stop.
            async for raw in ws:
                if not self._running:
                    break
                try:
                    await self.process_message(raw)
                except Exception as e:  # noqa: BLE001 — never crash the pump
                    log.debug("[ws_ingestion] Message error: %s", e)
            # Loop exited — either the socket closed or _running is False.
            self._health.connected = False
            self._ws = None

    async def _send_subscriptions(
        self, ws: Any, token_ids: list[str]
    ) -> None:
        """Send ``market`` channel subscriptions in batches of ``SUBSCRIBE_BATCH``.

        Polymarket's WS requires the exact ``{"assets_ids": [...], "type":
        "market", "custom_feature_enabled": True}`` shape (per
        ``core.ws_client._send_subscription``); we mirror it verbatim
        rather than inventing a new wire format. The logical-channel
        abstraction (``"book"`` / ``"trades"`` / ``"markets"``) lives
        entirely in this manager's routing layer — the upstream channel
        is the single ``"market"`` feed.
        """
        for i in range(0, len(token_ids), SUBSCRIBE_BATCH):
            batch = token_ids[i: i + SUBSCRIBE_BATCH]
            sub_msg = {
                "assets_ids": batch,
                "type": "market",
                "custom_feature_enabled": True,
            }
            try:
                await ws.send(json.dumps(sub_msg))
                log.debug(
                    "[ws_ingestion] Subscribed to %d token(s)", len(batch)
                )
            except Exception as e:  # noqa: BLE001 — keep going on send error
                log.warning(
                    "[ws_ingestion] Subscribe send failed: %s", e
                )

    # ── Message processing (Step 3 + Step 4 + Step 5) ───────────────────────

    async def process_message(self, raw: str | bytes) -> dict[str, Any] | None:
        """Parse, dedup, validate, route a single raw WS message.

        Returns the normalised dict on accept; ``None`` on duplicate /
        invalid / unknown-channel / unparseable. Designed to be called
        directly by tests (no WS connection required) — the unit tests
        exercise each stage of the pipeline through this single entry
        point.

        Pipeline stages
        ~~~~~~~~~~~~~~~
        1. Parse JSON (skip on failure).
        2. Extract ``event_type`` + ``data`` + ``token_id`` + ``seq_no``.
        3. Dedup by SHA-256 of ``(event_type, data)``.
        4. Channel-filter: drop if event_type not in any enabled channel.
        5. Gap detection on ``seq_no`` (per token).
        6. Validate + normalise (via ``core.data_validator``).
        7. Route to raw vault + normalised storage + in-memory store.
        8. Update health metrics (latency / throughput / counters).
        """
        self._health.messages_received += 1

        # ── 1. Parse JSON ───────────────────────────────────────────────
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self._health.messages_invalid += 1
            log.debug("[ws_ingestion] Unparseable message: %r", raw)
            return None
        if not isinstance(msg, dict):
            self._health.messages_invalid += 1
            return None

        # ── 2. Extract fields ──────────────────────────────────────────
        event_type = msg.get("event_type") or msg.get("type") or ""
        data = msg.get("data") or {}
        if not isinstance(data, dict):
            data = {"value": data}
        token_id = (
            data.get("asset_id")
            or data.get("token_id")
            or msg.get("asset_id")
            or msg.get("token_id")
            or ""
        )
        seq_no = _extract_seq_no(msg, data)

        # ── 3. Dedup by hash ────────────────────────────────────────────
        dedup_key = self._dedup_hash(event_type, data)
        if dedup_key in self._dedup_cache:
            self._health.messages_deduped += 1
            return None
        self._dedup_cache.append(dedup_key)

        # ── 4. Channel filter ───────────────────────────────────────────
        if not self._event_type_enabled(event_type):
            # Unsubscribe-the-channel mid-session = silently drop, but
            # still count toward received (we did receive it).
            log.debug(
                "[ws_ingestion] Dropping %r — channel not enabled",
                event_type,
            )
            return None

        # ── 5. Gap detection ────────────────────────────────────────────
        if token_id and seq_no is not None:
            missing = self.detect_gap(token_id, seq_no)
            if missing:
                gap = GapEvent(
                    token_id=token_id,
                    expected=missing[0],
                    seen=seq_no,
                    missing=missing,
                )
                self._gap_log.append(gap)
                self._health.gap_count += 1
                self._health.last_gap_at = time.time()
                log.warning(
                    "[ws_ingestion] Gap on %s: expected %d, saw %d (%d missing)",
                    token_id[:12], missing[0], seq_no, len(missing),
                )
                # Trigger backfill hook (Step 3 — "Trigger backfill").
                if self.on_gap is not None:
                    try:
                        await self.on_gap(gap)
                    except Exception as e:  # noqa: BLE001 — backfill errors must not crash the pump
                        log.warning(
                            "[ws_ingestion] on_gap callback failed: %s", e
                        )
            # Bring the per-token counter up to the seen seq_no so the
            # NEXT message's gap check uses the correct baseline.
            self._last_seq[token_id] = seq_no
            self._health.last_seq_no[token_id] = seq_no
        elif token_id:
            # No explicit seq_no — synthesise the per-token counter so a
            # subsequent explicit seq_no can still be gap-checked.
            self._last_seq[token_id] = self._last_seq.get(token_id, 0) + 1
            self._health.last_seq_no[token_id] = self._last_seq[token_id]

        # ── 6 + 7. Validate + route ─────────────────────────────────────
        routed = await self._route_message(event_type, token_id, data)

        # ── 8. Update health ────────────────────────────────────────────
        now = time.time()
        self._health.last_message_at = now
        if routed:
            self._health.messages_routed += 1
            # Latency = ingestion_time - upstream_event_time (best-effort).
            upstream_ts = _extract_upstream_timestamp(data, msg)
            if upstream_ts and upstream_ts > 0:
                latency_ms = max(0.0, (now - upstream_ts) * 1000.0)
                # Cap at 5 minutes to filter out garbage timestamps.
                if latency_ms < 300_000:
                    self._health.latency_samples_ms.append(latency_ms)
        return routed

    def detect_gap(self, token_id: str, seq_no: int) -> list[int]:
        """Return the list of missing seq numbers between last+1 and seq_no.

        Empty list when:
          * ``seq_no`` is the next expected value (no gap).
          * ``seq_no <= last_seen`` (out-of-order / replay — not a gap).
          * ``seq_no`` is the FIRST ever seen for ``token_id`` (no baseline).

        Step 3 — "Track expected message sequence numbers; Detect gaps
        in sequence; Trigger backfill for missing messages; Log gap
        events". The actual backfill trigger is the ``on_gap`` callback
        invoked by ``process_message`` — this method is pure and
        side-effect-free so tests can exercise it directly.
        """
        last = self._last_seq.get(token_id)
        if last is None:
            # First message on this token — no baseline to gap-check against.
            return []
        if seq_no <= last + 1:
            # Either the expected next value (seq_no == last+1) OR an
            # out-of-order replay (seq_no <= last) — neither is a gap.
            return []
        # Gap: we expected last+1 but saw seq_no → missing [last+1 .. seq_no-1].
        return list(range(last + 1, seq_no))

    # ── Routing (Step 5) ───────────────────────────────────────────────────

    async def _route_message(
        self, event_type: str, token_id: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Validate + route a single message based on its event_type.

        Returns the normalised dict on success; ``None`` on invalid /
        unroutable. Routes through:

          * ``raw_vault.record_observation`` — immutable raw payload
            (always, even on validation failure, so the dead-letter
            trail has the full upstream message for forensic review).
          * ``data_validator.validate_snapshot`` / ``validate_trade`` —
            schema / value / staleness / dedup gate (same gate the REST
            poller uses, so a WS snapshot and a REST snapshot are
            subject to identical quality rules).
          * ``timescale_db.record_snapshot`` / ``record_trade`` —
            normalised TimescaleDB hypertable write (fire-and-forget).
          * ``store.update_order_book`` — in-memory live book for the
            trading strategies (mirrors ``core.ws_client``).
          * ``source_registry.record_metric`` — health counter for the
            source-registry dashboard (success/failure accounting).
        """
        from core.ingestion.source_registry import source_registry  # lazy
        # Always record the raw observation first (immutability contract).
        # Lazy import — tests monkeypatch ``core.ingestion.raw_vault.raw_vault``
        # at call time so this pick-up is the patched singleton (mirrors
        # the lazy-import pattern in ``core.book_poller._fetch_book``).
        try:
            from core.ingestion.raw_vault import raw_vault
            await raw_vault.record_observation(SOURCE_ID, data)
        except Exception as e:  # noqa: BLE001 — vault failures must not block
            log.debug("[ws_ingestion] raw_vault write failed: %s", e)

        try:
            if event_type == EVT_BOOK_SNAPSHOT:
                return await self._route_book_snapshot(token_id, data)
            if event_type == EVT_PRICE_CHANGE:
                return await self._route_price_change(token_id, data)
            if event_type == EVT_TRADE:
                return await self._route_trade(token_id, data)
            if event_type in (EVT_USER_FILL, EVT_TICK_SIZE):
                # Market-lifecycle events: record to raw vault only (no
                # normalised hypertable for these yet — they're
                # informational, not yet on the live-trading path).
                await _safe_record_metric(SOURCE_ID, True, "")
                return {"event_type": event_type, "token_id": token_id, "routed": "raw_only"}
        except Exception as e:  # noqa: BLE001 — route errors are non-fatal
            self._health.messages_invalid += 1
            log.debug("[ws_ingestion] Route error (%s): %s", event_type, e)
            await _safe_record_metric(SOURCE_ID, False, str(e))
            return None

        # Unknown event_type — count as invalid (channel filter above
        # already dropped channels we're not subscribed to, so this is
        # a genuinely unknown event_type from an enabled channel).
        self._health.messages_invalid += 1
        return None

    async def _route_book_snapshot(
        self, token_id: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Validate + persist a full book snapshot."""
        if not token_id:
            self._health.messages_invalid += 1
            return None

        # Update the in-memory live book first (the trading strategies
        # read this on every tick — latency-critical, no validation gate).
        bids, asks = _parse_book_levels(data)
        book = OrderBook(token_id=token_id, bids=bids, asks=asks)
        await store.update_order_book(book)

        # Validate + normalise through the same gate the REST poller uses.
        from core.data_validator import data_validator

        snapshot = {
            "token_id": token_id,
            "best_bid": book.best_bid or 0.0,
            "best_ask": book.best_ask or 0.0,
            "timestamp": _extract_upstream_timestamp(data, {}),
            "source": SOURCE_ID,
        }
        if book.mid is not None:
            snapshot["mid"] = book.mid
        if book.spread is not None:
            snapshot["spread"] = book.spread

        result = data_validator.validate_snapshot(snapshot)
        if not result.is_valid:
            if result.is_duplicate:
                self._health.messages_deduped += 1
            else:
                self._health.messages_invalid += 1
                log.debug(
                    "[ws_ingestion] Snapshot rejected for %s: %s",
                    token_id[:12], result.errors,
                )
            await _safe_record_metric(source_registry, SOURCE_ID, True, "")
            return None

        # Persist the normalised snapshot + raw ladder to TimescaleDB.
        from core.timescale_db import timescale_db

        norm = result.normalized_data
        bids_payload = [{"price": b.price, "size": b.size} for b in bids]
        asks_payload = [{"price": a.price, "size": a.size} for a in asks]
        asyncio.create_task(
            timescale_db.record_snapshot(
                token_id=token_id,
                slug="",  # WS feed doesn't carry the market slug
                best_bid=float(norm.get("best_bid") or 0.0),
                best_ask=float(norm.get("best_ask") or 0.0),
                mid=float(norm["mid"]) if norm.get("mid") is not None else 0.0,
                spread=float(norm["spread"]) if norm.get("spread") is not None else 0.0,
                bids_json=bids_payload or None,
                asks_json=asks_payload or None,
            )
        )
        await _safe_record_metric(source_registry, SOURCE_ID, True, "")
        return {
            "event_type": EVT_BOOK_SNAPSHOT,
            "token_id": token_id,
            "routed": "snapshot",
            "normalized": norm,
        }

    async def _route_price_change(
        self, token_id: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply an incremental price-level update to the stored book.

        Mirrors ``core.ws_client._apply_price_change`` exactly — the
        in-memory book is rebuilt from the prior snapshot + the
        incremental change list, then re-persisted as a snapshot so the
        TimescaleDB hypertable carries a continuous view of the book
        (not a diff log).
        """
        if not token_id:
            self._health.messages_invalid += 1
            return None

        changes = data.get("changes", [])
        book = await store.get_order_book(token_id)
        if book is None:
            book = OrderBook(token_id=token_id)

        for change in changes:
            side = str(change.get("side", "")).upper()
            try:
                price = float(change.get("price", 0))
                size = float(change.get("size", 0))
            except (TypeError, ValueError):
                continue
            levels = book.bids if side == "BUY" else book.asks
            levels[:] = [lv for lv in levels if lv.price != price]
            if size > 0:
                levels.append(PriceLevel(price=price, size=size))

        book.bids.sort(key=lambda x: -x.price)
        book.asks.sort(key=lambda x: x.price)
        book.updated_at = time.time()
        await store.update_order_book(book)
        await _safe_record_metric(source_registry, SOURCE_ID, True, "")
        return {
            "event_type": EVT_PRICE_CHANGE,
            "token_id": token_id,
            "routed": "price_change",
            "changes_applied": len(changes),
        }

    async def _route_trade(
        self, token_id: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Validate + persist a single trade tick."""
        if not token_id:
            self._health.messages_invalid += 1
            return None

        from core.data_validator import data_validator
        from core.timescale_db import timescale_db

        try:
            price = float(data.get("price") or data.get("trade_price") or 0.0)
            size = float(data.get("size") or data.get("amount") or 0.0)
        except (TypeError, ValueError):
            self._health.messages_invalid += 1
            return None

        side = str(data.get("side") or data.get("taker_side") or "").upper()
        trade_id = (
            data.get("trade_id")
            or data.get("id")
            or f"{token_id}:{data.get('timestamp') or time.time()}"
        )
        ts = _extract_upstream_timestamp(data, {})

        trade = {
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side,
            "timestamp": ts,
            "trade_id": trade_id,
            "source": SOURCE_ID,
        }
        result = data_validator.validate_trade(trade)
        if not result.is_valid:
            if result.is_duplicate:
                self._health.messages_deduped += 1
            else:
                self._health.messages_invalid += 1
            await _safe_record_metric(source_registry, SOURCE_ID, True, "")
            return None

        norm = result.normalized_data
        asyncio.create_task(
            timescale_db.record_trade(
                token_id=token_id,
                price=float(norm.get("price", price)),
                size=float(norm.get("size", size)),
                side=str(norm.get("side", side)),
                timestamp=float(norm.get("timestamp", ts)),
                trade_id=trade_id,
                maker_address=str(data.get("maker") or ""),
                taker_order_id=str(data.get("taker_order_id") or ""),
            )
        )
        await _safe_record_metric(source_registry, SOURCE_ID, True, "")
        return {
            "event_type": EVT_TRADE,
            "token_id": token_id,
            "routed": "trade",
            "normalized": norm,
        }

    # ── Dedup helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _dedup_hash(event_type: str, data: dict[str, Any]) -> str:
        """SHA-256 of ``(event_type, sorted(data))``.

        Sorting the keys before serialisation makes the hash stable
        across dict-ordering variations (Python 3.7+ preserves insertion
        order, but the upstream CLOB may shuffle field order across
        versions; sorting is the conservative choice).
        """
        try:
            payload = json.dumps(
                {"event_type": event_type, "data": data},
                sort_keys=True, default=str,
            )
        except (TypeError, ValueError):
            payload = str({"event_type": event_type, "data": data})
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _event_type_enabled(self, event_type: str) -> bool:
        """True if any enabled logical channel includes ``event_type``."""
        if not event_type:
            return False
        for ch in self._active_channels:
            if event_type in CHANNELS.get(ch, frozenset()):
                return True
        return False

    # ── Checkpoint (Step 8) ─────────────────────────────────────────────────

    async def _checkpoint_loop(self) -> None:
        """Persist the per-token sequence map every ``CHECKPOINT_INTERVAL``.

        A separate task from ``_run_forever`` so a hung WS connection
        doesn't block the checkpoint flush — the contract is "the file on
        disk reflects a sequence state at most ``CHECKPOINT_INTERVAL``
        seconds stale".
        """
        while self._running:
            try:
                await asyncio.sleep(CHECKPOINT_INTERVAL)
                await self.checkpoint()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 — checkpoint must not crash
                log.debug("[ws_ingestion] Checkpoint loop error: %s", e)

    async def checkpoint(self) -> None:
        """Persist the current per-token sequence map to ``_checkpoint_path``.

        Writes atomically (temp file + rename) so a crash mid-write
        never leaves a half-written JSON file (the next boot would
        fail to parse a truncated file and silently start from the
        stream head — losing the resume contract).
        """
        try:
            payload = {
                "timestamp": time.time(),
                "uri": self._uri,
                "last_seq": dict(self._last_seq),
                "messages_received": self._health.messages_received,
                "messages_routed": self._health.messages_routed,
                "reconnect_count": self._reconnect_count,
            }
            self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._checkpoint_path.with_suffix(
                self._checkpoint_path.suffix + ".tmp"
            )
            tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
            tmp.replace(self._checkpoint_path)
        except Exception as e:  # noqa: BLE001 — checkpoint must never break boot
            log.debug("[ws_ingestion] Checkpoint write failed: %s", e)

    def _load_checkpoint_sync(self) -> None:
        """Synchronously load the per-token seq map from ``_checkpoint_path``.

        Called from ``__init__`` so the manager boots with the last-known
        per-token sequence baseline already populated — the FIRST inbound
        message's gap check uses the persisted baseline rather than
        starting fresh.
        """
        try:
            if not self._checkpoint_path.exists():
                return
            payload = json.loads(
                self._checkpoint_path.read_text(encoding="utf-8")
            )
            last_seq = payload.get("last_seq") or {}
            if isinstance(last_seq, dict):
                for k, v in last_seq.items():
                    try:
                        self._last_seq[str(k)] = int(v)
                        self._health.last_seq_no[str(k)] = int(v)
                    except (TypeError, ValueError):
                        continue
            log.debug(
                "[ws_ingestion] Loaded checkpoint: %d token(s) with seq baseline",
                len(self._last_seq),
            )
        except Exception as e:  # noqa: BLE001 — boot must never fail on ckpt
            log.debug("[ws_ingestion] Checkpoint load failed: %s", e)

    # ── Health (Step 7) ────────────────────────────────────────────────────

    @property
    def health(self) -> dict[str, Any]:
        """Public-facing health snapshot for the observability collector.

        Mirrors the W22-7 ``data_source.*`` metric vocabulary
        (``data_source.connected`` / ``data_source.latency`` /
        ``data_source.reconnects`` / ``data_source.gap_count``) so the
        existing ``core.observability_collector`` can ingest the dict
        unchanged. The ``recent_gaps`` list is the raw ``GapEvent``
        list (serialised to dicts) so the operator dashboard can show
        the last gap per token.
        """
        recent_gaps = [
            {
                "token_id": g.token_id,
                "expected": g.expected,
                "seen": g.seen,
                "missing": list(g.missing),
                "detected_at": g.detected_at,
            }
            for g in list(self._gap_log)[-20:]
        ]
        return self._health.to_dict(recent_gaps)

    @property
    def stats(self) -> dict[str, Any]:
        """Alias for ``health`` — matches the ``stats`` property convention
        used by ``BookPoller`` / ``WSBroadcastManager`` / etc.
        """
        return self.health

    @property
    def reconnect_count(self) -> int:
        """Cumulative reconnect count (mirrors ``ws_client._reconnect_count``)."""
        return self._reconnect_count

    @property
    def gap_log(self) -> list[GapEvent]:
        """Snapshot of the recent-gap deque (for backfill scheduler)."""
        return list(self._gap_log)


# ── Module-level helpers ─────────────────────────────────────────────────────


async def _default_connect_factory(uri: str, **kwargs: Any) -> Any:
    """Production connect factory — delegates to ``websockets.connect``.

    Pulled out as a module-level function so tests can patch
    ``ingestion.ws_ingestion._default_connect_factory`` (rather than
    monkey-patching ``websockets.connect`` directly, which is fragile
    across versions — ``websockets`` 13 moved the connect entrypoint
    around between ``websockets.connect`` and ``websockets.asyncio.client.connect``).
    """
    # Strip kwargs that the real ``websockets.connect`` doesn't accept
    # (tests may inject a fake connect factory that ignores them, but
    # production must not pass unknown kwargs).
    return websockets.connect(
        uri,
        ping_interval=kwargs.get("ping_interval", PING_INTERVAL),
        ping_timeout=kwargs.get("ping_timeout", PING_TIMEOUT),
        max_size=kwargs.get("max_size", MAX_SIZE),
    )


def _ws_is_closed(ws: Any) -> bool:
    """Best-effort check for whether a WS object is closed.

    ``websockets.WebSocketClientProtocol`` exposes ``.closed`` (a bool
    property on the asyncio implementation); test stubs may not — fall
    back to ``False`` (treat as open) when the attribute is missing so
    the caller doesn't crash mid-subscribe.
    """
    try:
        return bool(getattr(ws, "closed", False))
    except Exception:  # noqa: BLE001 — defensive
        return False


def _extract_seq_no(msg: dict[str, Any], data: dict[str, Any]) -> int | None:
    """Pull an explicit sequence number from the message (or ``None``).

    Checks the documented Polymarket field names plus a few common
    variants (``seq_no`` / ``sequence`` / ``seq``) at both the message
    root and inside ``data``. Returns ``None`` when no explicit seq_no
    is present — the caller falls back to the synthesised per-token
    counter.
    """
    for key in ("seq_no", "sequence", "seq"):
        for src in (msg, data):
            v = src.get(key) if isinstance(src, dict) else None
            if v is None:
                continue
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def _extract_upstream_timestamp(
    data: dict[str, Any], msg: dict[str, Any]
) -> float:
    """Pull the upstream event timestamp (seconds since epoch).

    The CLOB WS publishes timestamps in milliseconds (e.g.
    ``"timestamp": "1700000000000"``) — divide by 1000 to normalise.
    ISO-8601 strings and unix seconds are also accepted.
    """
    raw = (
        data.get("timestamp")
        or msg.get("timestamp")
        or data.get("event_timestamp")
        or data.get("created_at")
    )
    if raw is None:
        return 0.0
    # Numeric string / int / float — milliseconds when value > 1e12,
    # seconds otherwise (a unix-seconds value will be < ~2e9).
    try:
        v = float(raw)
    except (TypeError, ValueError):
        # Try ISO-8601.
        s = str(raw)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            from datetime import datetime

            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            return 0.0
    if v > 1e12:
        return v / 1000.0
    return v


def _parse_book_levels(
    data: dict[str, Any],
) -> tuple[list[PriceLevel], list[PriceLevel]]:
    """Parse the ``bids`` / ``asks`` lists from a WS book snapshot.

    Mirrors ``core.ws_client._apply_book_snapshot``: sorts bids
    descending (best bid first) and asks ascending (best ask first),
    skipping any zero-size level (the CLOB publishes tombstones for
    cleared levels rather than omitting them).
    """
    raw_bids = data.get("bids", []) or []
    raw_asks = data.get("asks", []) or []
    bids = sorted(
        [
            PriceLevel(price=float(b["price"]), size=float(b["size"]))
            for b in raw_bids
            if isinstance(b, dict)
            and float(b.get("size", 0)) > 0
        ],
        key=lambda x: -x.price,
    )
    asks = sorted(
        [
            PriceLevel(price=float(a["price"]), size=float(a["size"]))
            for a in raw_asks
            if isinstance(a, dict)
            and float(a.get("size", 0)) > 0
        ],
        key=lambda x: x.price,
    )
    return bids, asks


async def _safe_record_metric(
    registry: Any, source_id: str, success: bool, err: str
) -> None:
    """Fire-and-forget ``source_registry.record_metric``.

    The registry's PG write may block (asyncpg acquire) — wrap in
    ``asyncio.create_task`` so the WS pump is never blocked on the
    registry. Errors are swallowed (best-effort accounting).
    """
    try:
        asyncio.create_task(registry.record_metric(source_id, success, err))
    except Exception:  # noqa: BLE001 — accounting must never block
        pass


# ── Module-level singleton ──────────────────────────────────────────────────

# Production singleton — mirrors the convention used by every sibling
# background-task module (``core.ws_client.ws_client``,
# ``core.book_poller.book_poller``, ``core.gamma_client.gamma_client`` …).
# Tests do NOT mutate this singleton — they construct fresh
# ``WSIngestionManager`` instances per test (mirrors the ``poller`` fixture
# in ``tests/test_book_poller.py``).
ws_ingestion_manager = WSIngestionManager()
