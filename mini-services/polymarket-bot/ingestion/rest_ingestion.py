"""REST ingestion fallback — adaptive polling when WebSocket is unavailable.

W31-2 — companion to ``ingestion.ws_ingestion``. When the CLOB
WebSocket is down (sustained outage, exhaust-reconnect-give-up,
operator-disabled), this module picks up the slack by polling the
CLOB REST API at adaptive intervals:

  * **Active markets** — polled every ``ACTIVE_INTERVAL`` (1 s by
    default, matching the WS feed's tick rate). A market is
    "active" when it has traded in the last 5 minutes OR is on the
    operator-supplied promote list.
  * **Inactive markets** — polled every ``INACTIVE_INTERVAL``
    (30 s). Cheaper cadence for the long tail of markets that
    haven't traded today but still need top-of-book freshness for
    the screener.
  * **Gamma metadata** — polled every ``GAMMA_INTERVAL`` (5 min)
    for market metadata: new markets are detected by comparing the
    ``condition_id`` set against the last poll; market resolution
    events are detected by comparing the ``closed`` /
    ``resolvedBy`` fields against the last poll.

The poller is designed to be a drop-in replacement for
``WSIngestionManager`` at the routing layer — both produce the same
``record_snapshot`` / ``record_trade`` calls to
``core.timescale_db.timescale_db``, so the downstream hypertable
schema is identical regardless of which path produced the row. The
``source`` field is ``"clob_rest"`` for REST-sourced snapshots and
``"clob_ws"`` for WS-sourced ones, so the data-quality dashboard can
attribute per-token staleness to the right path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from config import settings
from core.clob_client import clob_client
from core.data_store import OrderBook, PriceLevel, store
from core.gamma_client import gamma_client
from core.ingestion.raw_vault import raw_vault
from core.ingestion.source_registry import source_registry

log = logging.getLogger(__name__)

# ── Adaptive intervals ───────────────────────────────────────────────────────
ACTIVE_INTERVAL = 1.0       # seconds — active-market polling cadence (1 Hz)
INACTIVE_INTERVAL = 30.0    # seconds — inactive-market polling cadence
GAMMA_INTERVAL = 300.0      # seconds — Gamma metadata refresh (5 min)
ACTIVE_TRADE_RECENCY_S = 300.0   # a market is "active" if it traded within 5 min

REST_SOURCE = "clob_rest"
GAMMA_SOURCE = "gamma_api"

# Circuit-breaker trip threshold (mirrors ``core.book_poller``'s pattern
# so a sustained outage of the REST endpoint doesn't burn through the
# poller's request budget). After ``CIRCUIT_TRIP_ERRORS`` consecutive
# errors, pause polling for ``CIRCUIT_COOLDOWN_S`` seconds.
CIRCUIT_TRIP_ERRORS = 5
CIRCUIT_COOLDOWN_S = 30.0


@dataclass
class _MarketState:
    """Per-token adaptive polling state.

    Tracks the last successful poll + the last observed trade timestamp
    so the adaptive scheduler can decide whether the token is "active"
    (1 s cadence) or "inactive" (30 s cadence). Mirrors the
    ``_tier1_tokens`` / ``_tier2_tokens`` split in
    ``core.book_poller`` but with per-token dynamism — a token promoted
    to active by a recent trade is demoted back to inactive after
    ``ACTIVE_TRADE_RECENCY_S`` seconds of no trades.
    """

    last_polled: float = 0.0          # last successful REST poll timestamp
    last_trade_at: float = 0.0       # last observed trade timestamp (0 = never)
    last_best_bid: float = 0.0        # last best bid seen (for delta detection)
    last_best_ask: float = 0.0        # last best ask seen
    poll_count: int = 0               # successful polls
    error_count: int = 0              # consecutive errors (for circuit breaker)


@dataclass
class _GammaState:
    """Per-condition_id Gamma metadata state.

    Tracks the last-seen ``closed`` / ``resolvedBy`` fields so the
    Gamma poller can detect new markets (condition_id not in the map)
    and resolution events (``closed`` flipped from False → True, or
    ``resolvedBy`` populated from empty).
    """

    condition_id: str
    question: str = ""
    closed: bool = False
    resolved_by: str = ""
    first_seen_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)


@dataclass
class _RESTHealth:
    """Mutable REST-ingestion health counters."""

    book_polls: int = 0
    book_errors: int = 0
    gamma_polls: int = 0
    gamma_errors: int = 0
    new_markets_detected: int = 0
    resolutions_detected: int = 0
    circuit_open: bool = False
    circuit_open_until: float = 0.0
    last_book_poll_at: float = 0.0
    last_gamma_poll_at: float = 0.0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "book_polls": self.book_polls,
            "book_errors": self.book_errors,
            "gamma_polls": self.gamma_polls,
            "gamma_errors": self.gamma_errors,
            "new_markets_detected": self.new_markets_detected,
            "resolutions_detected": self.resolutions_detected,
            "circuit_open": self.circuit_open,
            "circuit_open_until": self.circuit_open_until,
            "last_book_poll_at": self.last_book_poll_at,
            "last_gamma_poll_at": self.last_gamma_poll_at,
            "last_error": self.last_error,
        }


class RESTIngestionFallback:
    """Adaptive REST polling fallback for the WS ingestion manager.

    Lifecycle
    ~~~~~~~~~
    1. ``start()`` — kicks off two background tasks:
       ``_book_poll_loop`` (per-token adaptive polling) and
       ``_gamma_poll_loop`` (5-minute Gamma metadata refresh).
    2. ``stop()`` — flips ``_running`` to False, cancels both tasks.

    The poller is designed to be invoked directly by tests via
    ``poll_once()`` (single-shot CLOB poll) / ``poll_gamma_once()``
    (single-shot Gamma poll) / ``detect_new_markets()`` /
    ``detect_resolutions()`` — no background task required.
    """

    def __init__(
        self,
        *,
        active_interval: float = ACTIVE_INTERVAL,
        inactive_interval: float = INACTIVE_INTERVAL,
        gamma_interval: float = GAMMA_INTERVAL,
        max_concurrent: int = 12,
    ) -> None:
        self._active_interval = active_interval
        self._inactive_interval = inactive_interval
        self._gamma_interval = gamma_interval
        self._sem = asyncio.Semaphore(max_concurrent)
        self._running: bool = False
        self._book_task: asyncio.Task | None = None
        self._gamma_task: asyncio.Task | None = None
        # Per-token adaptive state — populated by ``add_tokens`` /
        # ``promote_active``; the polling loop reads it to decide the
        # per-token cadence.
        self._token_state: dict[str, _MarketState] = {}
        # Per-condition_id Gamma metadata — populated by
        # ``poll_gamma_once``; the new-market / resolution detectors
        # diff against it.
        self._gamma_state: dict[str, _GammaState] = {}
        # Consecutive-error counter for the circuit breaker.
        self._consecutive_errors: int = 0
        self._health = _RESTHealth()

    # ── Token management ──────────────────────────────────────────────────

    def add_tokens(self, token_ids: list[str]) -> None:
        """Register tokens for adaptive polling.

        New tokens are initialised with ``last_polled=0`` so the next
        poll loop tick polls them immediately rather than waiting for
        the per-token cadence.
        """
        for tid in token_ids:
            if tid and tid not in self._token_state:
                self._token_state[tid] = _MarketState()

    def promote_active(self, token_ids: list[str]) -> None:
        """Mark tokens as active (1 s cadence) regardless of trade recency.

        Used by the live-trading path to force-promote tokens that the
        market maker is quoting (we want top-of-book freshness for the
        tokens we're actively quoting, even if they haven't traded in
        the last 5 minutes).
        """
        for tid in token_ids:
            state = self._token_state.get(tid)
            if state is None:
                state = _MarketState()
                self._token_state[tid] = state
            # Touch last_trade_at so the adaptive scheduler treats this
            # token as active for the next ACTIVE_TRADE_RECENCY_S seconds.
            state.last_trade_at = time.time()

    def demote_inactive(self, token_ids: list[str]) -> None:
        """Mark tokens as inactive (30 s cadence) regardless of trade recency.

        Used by the live-trading path when a strategy stops quoting a
        token — we don't want to keep polling it at 1 Hz once nobody's
        reading the data.
        """
        for tid in token_ids:
            state = self._token_state.get(tid)
            if state is not None:
                state.last_trade_at = 0.0

    def remove_tokens(self, token_ids: list[str]) -> None:
        """Stop polling tokens (e.g. on market resolution)."""
        for tid in token_ids:
            self._token_state.pop(tid, None)

    @property
    def tracked_tokens(self) -> list[str]:
        return list(self._token_state.keys())

    @property
    def active_tokens(self) -> list[str]:
        """Tokens currently classified as active (1 s cadence)."""
        now = time.time()
        return [
            tid for tid, s in self._token_state.items()
            if s.last_trade_at > 0
            and now - s.last_trade_at < ACTIVE_TRADE_RECENCY_S
        ]

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the two background poll loops."""
        if self._running:
            return
        self._running = True
        self._book_task = asyncio.create_task(
            self._book_poll_loop(), name="rest-ingestion-books"
        )
        self._gamma_task = asyncio.create_task(
            self._gamma_poll_loop(), name="rest-ingestion-gamma"
        )
        log.info(
            "[rest_ingestion] Started (active=%.1fs, inactive=%.1fs, gamma=%.0fs, tokens=%d)",
            self._active_interval,
            self._inactive_interval,
            self._gamma_interval,
            len(self._token_state),
        )

    async def stop(self) -> None:
        """Gracefully stop both poll loops."""
        self._running = False
        for t in (self._book_task, self._gamma_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception as e:  # noqa: BLE001 — shutdown must not raise
                    log.debug("[rest_ingestion] Stop caught: %s", e)
        log.info("[rest_ingestion] Stopped")

    # ── Book polling loop ─────────────────────────────────────────────────

    async def _book_poll_loop(self) -> None:
        """Adaptive per-token polling loop.

        On each iteration, partitions the tracked tokens into "active"
        (poll at ``_active_interval``) and "inactive" (poll at
        ``_inactive_interval``) based on the per-token trade recency.
        Honours the circuit breaker — when OPEN, pauses for
        ``CIRCUIT_COOLDOWN_S`` seconds rather than burning through the
        request budget.
        """
        # Stagger the first tick so the loop doesn't fire on boot before
        # the WS manager has had a chance to claim the tokens.
        await asyncio.sleep(0.5)
        while self._running:
            try:
                # Circuit breaker: pause if OPEN and not yet past cooldown.
                if self._health.circuit_open:
                    if time.time() < self._health.circuit_open_until:
                        await asyncio.sleep(self._active_interval)
                        continue
                    self._health.circuit_open = False
                    self._consecutive_errors = 0
                    log.info(
                        "[rest_ingestion] Circuit CLOSED — resuming polling"
                    )

                # Partition tokens into active / inactive based on
                # trade recency.
                now = time.time()
                active: list[str] = []
                inactive: list[str] = []
                for tid, state in self._token_state.items():
                    if state.last_trade_at > 0 and now - state.last_trade_at < ACTIVE_TRADE_RECENCY_S:
                        active.append(tid)
                    else:
                        inactive.append(tid)

                # Poll each tier in parallel, gated by the semaphore.
                await self._poll_tier(active, self._active_interval)
                await self._poll_tier(inactive, self._inactive_interval)

                # Sleep for the shorter interval so the active tier
                # polls at its 1 Hz cadence (the inactive tier will be
                # polled again on the next iteration of this loop).
                await asyncio.sleep(self._active_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 — loop must not crash
                log.debug("[rest_ingestion] Book poll loop error: %s", e)
                await asyncio.sleep(self._active_interval)

    async def _poll_tier(self, tokens: list[str], interval: float) -> None:
        """Poll a batch of tokens in parallel, gated by ``self._sem``."""
        if not tokens:
            return

        async def fetch_one(tid: str) -> None:
            async with self._sem:
                state = self._token_state.get(tid)
                if state is None:
                    return
                # Skip if polled recently enough (per-token cadence).
                now = time.time()
                if state.last_polled > 0 and now - state.last_polled < interval:
                    return
                await self._poll_book(tid)

        await asyncio.gather(
            *(fetch_one(t) for t in tokens), return_exceptions=True
        )

    async def _poll_book(self, token_id: str) -> bool:
        """Fetch + persist a single token's book via CLOB REST.

        Returns True on success, False on failure. Updates the
        per-token state + the global health counters.
        """
        try:
            data = await clob_client.get_order_book(token_id)
        except Exception as e:  # noqa: BLE001 — network failure
            self._consecutive_errors += 1
            self._health.book_errors += 1
            self._health.last_error = str(e)
            state = self._token_state.get(token_id)
            if state is not None:
                state.error_count += 1
            await _safe_record_metric(source_registry, REST_SOURCE, False, str(e))
            self._maybe_trip_circuit()
            return False

        if not data:
            self._consecutive_errors += 1
            self._health.book_errors += 1
            self._maybe_trip_circuit()
            return False

        await self._apply_book(token_id, data)
        self._consecutive_errors = 0
        self._health.book_polls += 1
        self._health.last_book_poll_at = time.time()
        state = self._token_state.get(token_id)
        if state is not None:
            state.last_polled = self._health.last_book_poll_at
            state.poll_count += 1
        await _safe_record_metric(source_registry, REST_SOURCE, True, "")
        return True

    async def _apply_book(self, token_id: str, data: dict) -> None:
        """Persist a REST-sourced book snapshot.

        Mirrors ``core.book_poller._apply_book`` — parses the ladder,
        updates the in-memory ``store``, validates through
        ``data_validator``, and persists to TimescaleDB. The
        ``source`` field is ``REST_SOURCE`` (``"clob_rest"``) so the
        data-quality dashboard can attribute per-token staleness to
        the REST path (vs. the WS path which sets ``"clob_ws"``).
        """
        raw_bids = data.get("bids", []) or []
        raw_asks = data.get("asks", []) or []
        bids = sorted(
            [
                PriceLevel(price=float(b["price"]), size=float(b["size"]))
                for b in raw_bids
                if isinstance(b, dict) and float(b.get("size", 0)) > 0
            ],
            key=lambda x: -x.price,
        )
        asks = sorted(
            [
                PriceLevel(price=float(a["price"]), size=float(a["size"]))
                for a in raw_asks
                if isinstance(a, dict) and float(a.get("size", 0)) > 0
            ],
            key=lambda x: x.price,
        )
        book = OrderBook(
            token_id=token_id, bids=bids, asks=asks, updated_at=time.time()
        )
        await store.update_order_book(book)

        # Record the raw observation (immutability contract — mirrors
        # the WS path).
        try:
            await raw_vault.record_observation(REST_SOURCE, data)
        except Exception as e:  # noqa: BLE001
            log.debug("[rest_ingestion] raw_vault write failed: %s", e)

        # Validate + persist (same gate as the WS path).
        from core.data_validator import data_validator
        from core.timescale_db import timescale_db

        raw_snapshot = {
            "token_id": token_id,
            "best_bid": book.best_bid or 0.0,
            "best_ask": book.best_ask or 0.0,
            "timestamp": data.get("timestamp") or book.updated_at,
            "source": REST_SOURCE,
        }
        if book.mid is not None:
            raw_snapshot["mid"] = book.mid
        if book.spread is not None:
            raw_snapshot["spread"] = book.spread

        result = data_validator.validate_snapshot(raw_snapshot)
        if not result.is_valid:
            if not result.is_duplicate:
                log.debug(
                    "[rest_ingestion] Snapshot rejected for %s: %s",
                    token_id[:12], result.errors,
                )
            return

        norm = result.normalized_data
        bids_payload = [{"price": b.price, "size": b.size} for b in bids]
        asks_payload = [{"price": a.price, "size": a.size} for a in asks]
        asyncio.create_task(
            timescale_db.record_snapshot(
                token_id=token_id,
                slug="",
                best_bid=float(norm.get("best_bid") or 0.0),
                best_ask=float(norm.get("best_ask") or 0.0),
                mid=float(norm["mid"]) if norm.get("mid") is not None else 0.0,
                spread=float(norm["spread"]) if norm.get("spread") is not None else 0.0,
                bids_json=bids_payload or None,
                asks_json=asks_payload or None,
            )
        )

        # Track per-token best bid/ask so we can detect a tick (the
        # promote/demote logic uses trade recency, but the delta
        # detection is useful for the data-quality dashboard).
        state = self._token_state.get(token_id)
        if state is not None:
            state.last_best_bid = book.best_bid or 0.0
            state.last_best_ask = book.best_ask or 0.0

    def _maybe_trip_circuit(self) -> None:
        """Trip the circuit breaker after ``CIRCUIT_TRIP_ERRORS`` consecutive errors."""
        if (
            self._consecutive_errors >= CIRCUIT_TRIP_ERRORS
            and not self._health.circuit_open
        ):
            self._health.circuit_open = True
            self._health.circuit_open_until = time.time() + CIRCUIT_COOLDOWN_S
            log.warning(
                "[rest_ingestion] Circuit OPEN — %d consecutive errors, pausing %.0fs",
                self._consecutive_errors, CIRCUIT_COOLDOWN_S,
            )

    # ── Gamma polling loop ────────────────────────────────────────────────

    async def _gamma_poll_loop(self) -> None:
        """Poll Gamma for market metadata every ``_gamma_interval`` seconds.

        Detects:
          * New markets — ``condition_id`` not in ``_gamma_state``.
          * Market resolution events — ``closed`` flipped False → True.
        """
        # Stagger the first tick so the loop doesn't fire on boot.
        await asyncio.sleep(2.0)
        while self._running:
            try:
                await self.poll_gamma_once()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 — loop must not crash
                log.debug("[rest_ingestion] Gamma poll loop error: %s", e)
            # Sleep for the full interval — Gamma is a metadata API,
            # not a tick feed.
            await asyncio.sleep(self._gamma_interval)

    async def poll_gamma_once(self) -> list[dict]:
        """Single-shot Gamma poll + new-market / resolution detection.

        Returns the list of markets fetched (used by tests to verify
        the poll ran). Side effects:
          * Updates ``_gamma_state`` with the latest metadata.
          * Calls ``self.on_new_market`` / ``self.on_resolution`` hooks
            for each detected event (if registered).
        """
        try:
            markets = await gamma_client.get_markets(
                active=True, closed=False, limit=200
            )
        except Exception as e:  # noqa: BLE001
            self._health.gamma_errors += 1
            self._health.last_error = str(e)
            await _safe_record_metric(source_registry, GAMMA_SOURCE, False, str(e))
            return []

        self._health.gamma_polls += 1
        self._health.last_gamma_poll_at = time.time()
        await _safe_record_metric(source_registry, GAMMA_SOURCE, True, "")

        # Detect new markets + resolutions against the prior state.
        new_markets = self.detect_new_markets(markets)
        resolutions = self.detect_resolutions(markets)

        # Update the per-condition_id state with the latest snapshot.
        now = time.time()
        for m in markets:
            cid = m.get("conditionId") or m.get("condition_id") or ""
            if not cid:
                continue
            prev = self._gamma_state.get(cid)
            if prev is None:
                self._gamma_state[cid] = _GammaState(
                    condition_id=cid,
                    question=str(m.get("question") or "")[:200],
                    closed=bool(m.get("closed") or m.get("resolvedBy")),
                    resolved_by=str(m.get("resolvedBy") or ""),
                    first_seen_at=now,
                    last_seen_at=now,
                )
            else:
                prev.closed = bool(m.get("closed") or False)
                prev.resolved_by = str(m.get("resolvedBy") or "")
                prev.last_seen_at = now

        return markets

    def detect_new_markets(self, current: list[dict]) -> list[dict]:
        """Return markets in ``current`` not previously seen.

        Side effect: increments ``_health.new_markets_detected``.
        Belt-and-braces: only counts a market as "new" when its
        ``conditionId`` is non-empty AND not already in
        ``_gamma_state``. Belt-and-braces against the race where the
        Gamma API returns a market mid-resolution (closed=True) — those
        are skipped (a market that resolves before we ever saw it
        active is not a "new market" event, it's a missed-active event).
        """
        new: list[dict] = []
        for m in current:
            cid = m.get("conditionId") or m.get("condition_id") or ""
            if not cid or cid in self._gamma_state:
                continue
            if m.get("closed") or m.get("resolvedBy"):
                # Skip resolved-before-seen markets.
                continue
            new.append(m)
        if new:
            self._health.new_markets_detected += len(new)
            log.info(
                "[rest_ingestion] Detected %d new market(s)",
                len(new),
            )
        return new

    def detect_resolutions(self, current: list[dict]) -> list[dict]:
        """Return markets in ``current`` that flipped ``closed`` False → True.

        Side effect: increments ``_health.resolutions_detected``.
        Belt-and-braces: only counts as a resolution when the market
        was previously seen AND was previously ``closed=False`` AND is
        now ``closed=True`` (or has ``resolvedBy`` populated). A market
        we never saw active is NOT a resolution event (see
        ``detect_new_markets``).
        """
        resolutions: list[dict] = []
        for m in current:
            cid = m.get("conditionId") or m.get("condition_id") or ""
            if not cid:
                continue
            prev = self._gamma_state.get(cid)
            if prev is None:
                continue
            now_closed = bool(m.get("closed") or False)
            now_resolved = bool(m.get("resolvedBy"))
            if (not prev.closed) and (now_closed or now_resolved):
                resolutions.append(m)
        if resolutions:
            self._health.resolutions_detected += len(resolutions)
            log.info(
                "[rest_ingestion] Detected %d market resolution(s)",
                len(resolutions),
            )
        return resolutions

    # ── One-shot poll helper (for tests + on-demand refresh) ─────────────

    async def poll_once(self) -> dict[str, bool]:
        """Single-shot CLOB REST poll of every tracked token.

        Returns ``{token_id: success_bool}`` for each token polled.
        Used by tests to exercise the polling path without spinning up
        the background loop. Production callers should use ``start()``
        instead — the loop is adaptive (per-token cadence) and parallel.
        """
        tokens = list(self._token_state.keys())
        results: dict[str, bool] = {}
        if not tokens:
            return results

        async def fetch_one(tid: str) -> tuple[str, bool]:
            async with self._sem:
                ok = await self._poll_book(tid)
                return tid, ok

        gathered = await asyncio.gather(
            *(fetch_one(t) for t in tokens), return_exceptions=True
        )
        for r in gathered:
            if isinstance(r, Exception):
                continue
            tid, ok = r
            results[tid] = ok
        return results

    # ── Health ─────────────────────────────────────────────────────────────

    @property
    def health(self) -> dict[str, Any]:
        """Public-facing health snapshot for the observability collector."""
        out = self._health.to_dict()
        out["tracked_tokens"] = len(self._token_state)
        out["active_tokens"] = len(self.active_tokens)
        out["known_markets"] = len(self._gamma_state)
        out["intervals"] = {
            "active_s": self._active_interval,
            "inactive_s": self._inactive_interval,
            "gamma_s": self._gamma_interval,
        }
        return out

    @property
    def stats(self) -> dict[str, Any]:
        """Alias for ``health`` — matches the ``stats`` convention."""
        return self.health

    @property
    def gamma_state(self) -> dict[str, _GammaState]:
        """Direct accessor for tests — the per-condition_id Gamma map."""
        return self._gamma_state


# ── Module-level helpers (mirrors ws_ingestion) ─────────────────────────────


async def _safe_record_metric(
    registry: Any, source_id: str, success: bool, err: str
) -> None:
    """Fire-and-forget ``source_registry.record_metric``.

    Same contract as ``ingestion.ws_ingestion._safe_record_metric`` —
    the registry's PG write may block; wrap in ``asyncio.create_task``
    so the poll loop is never blocked on the registry.
    """
    try:
        asyncio.create_task(registry.record_metric(source_id, success, err))
    except Exception:  # noqa: BLE001 — accounting must never block
        pass


# ── Module-level singleton ──────────────────────────────────────────────────

# Production singleton — mirrors ``ws_ingestion_manager``. Tests do NOT
# mutate this singleton; they construct fresh ``RESTIngestionFallback``
# instances per test (mirrors the ``poller`` fixture in
# ``tests/test_book_poller.py``).
rest_ingestion_fallback = RESTIngestionFallback()
