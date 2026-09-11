"""Source connectors for the unified ingestion pipeline.

Each connector wraps an existing ``core.*`` client (``core.clob_client``,
``core.gamma_client``, ``core.ws_client`` …) and forwards every fetched
payload through the pipeline. The connectors add:

  * A **name** / **source type** / **config** triple (so the pipeline
    can tag records with ``source="clob"`` / ``source="gamma"`` / …).
  * **Health metrics** (request count, success rate, latency, last
    error) surfaced via ``get_health()`` so the observability layer
    can render connector status.
  * **Rate-limit / retry / circuit-breaker** delegation to the existing
    ``core.api_resilience`` / ``core.circuit_breaker`` infrastructure
    (the connectors do NOT re-implement resilience — they call the
    underlying client, which is already wired through the
    resilience layer).
  * **Async fetch + sync push** — every connector's ``fetch_*`` method
    is async (the underlying clients are async); every connector
    pushes the fetched payload through ``pipeline.process`` via
    ``asyncio.to_thread`` so the pipeline's sync validator doesn't
    block the event loop.

Connector roster
----------------
  * ``ClobRestConnector``         — Polymarket CLOB REST API. Fetches
    order books (``/book``), public trades (``/trades``), and
    per-token last-trade-price. Each fetch is forwarded as a
    ``order_book`` / ``trade`` / ``snapshot`` record.
  * ``GammaRestConnector``        — Polymarket Gamma REST API.
    Fetches market metadata (``/markets``), per-condition-id
    markets, and events. Each fetch is forwarded as a
    ``market_info`` record.
  * ``WebSocketConnector``        — Polymarket CLOB WebSocket
    (``wss://ws-subscriptions-clob.polymarket.com/ws/market``).
    Streams real-time book / price-change / trade events. Each
    inbound message is forwarded as a ``snapshot`` / ``trade``
    record.
  * ``NewsSentimentConnector``    — News / sentiment feed. Stub —
    no concrete upstream is wired yet (a future wave will plug a
    real RSS / Twitter / news-API source). The connector's contract
    (``fetch_headlines`` / ``fetch_sentiment``) is in place so a
    downstream consumer can be developed in parallel.

Connector registry
------------------
The ``connector_registry`` singleton tracks every instantiated
connector so the observability layer can render a single connector-
health dashboard (and so a future wave can iterate every connector
for a unified ``health_check`` endpoint).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from ingestion.pipeline import Pipeline, pipeline

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

# Health metrics window — bounded so a long-running process can't grow
# the latency deque unbounded.
_HEALTH_WINDOW = 100


# ── Health metrics ────────────────────────────────────────────────────────────


@dataclass
class ConnectorHealth:
    """Health metrics for a single connector.

    All counters are cumulative since the connector's construction
    (reset only by ``reset_health``). Latencies are the round-trip
    durations of the last ``_HEALTH_WINDOW`` successful fetches.
    """

    requests_total: int = 0
    success_count: int = 0
    error_count: int = 0
    last_success_at: float = 0.0
    last_error_at: float = 0.0
    last_error_msg: str = ""
    last_latency_ms: float = 0.0
    latencies_ms: deque = field(default_factory=lambda: deque(maxlen=_HEALTH_WINDOW))

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view (for the observability API)."""
        n = len(self.latencies_ms)
        avg = (sum(self.latencies_ms) / n) if n else 0.0
        return {
            "requests_total": self.requests_total,
            "success_count": self.success_count,
            "error_count": self.error_count,
            "last_success_at": self.last_success_at or None,
            "last_error_at": self.last_error_at or None,
            "last_error_msg": self.last_error_msg,
            "last_latency_ms": round(self.last_latency_ms, 2),
            "avg_latency_ms": round(avg, 2),
            "samples": n,
        }

    def reset(self) -> None:
        """Zero every counter + clear the latency deque (test-only)."""
        self.requests_total = 0
        self.success_count = 0
        self.error_count = 0
        self.last_success_at = 0.0
        self.last_error_at = 0.0
        self.last_error_msg = ""
        self.last_latency_ms = 0.0
        self.latencies_ms.clear()


# ── Base connector ────────────────────────────────────────────────────────────


class BaseConnector:
    """Base class for every connector.

    Subclasses implement the source-specific ``fetch_*`` methods. The
    base class handles:

      * Health metric tracking (``_record_success`` / ``_record_error``).
      * Pipeline dispatch (``_push`` — runs ``pipeline.process`` in
        ``asyncio.to_thread`` so the sync validator doesn't block
        the event loop).
      * Connector-registry registration (the constructor adds ``self``
        to the global ``connector_registry`` so the observability
        layer can find every connector).
    """

    #: Connector name (e.g. ``"clob_rest"``). Subclasses override.
    name: str = "base"

    #: Source label passed to ``pipeline.process`` (e.g. ``"clob"``).
    #: Kept distinct from ``name`` so a single connector can emit
    #: multiple sources (e.g. a CLOB REST + CLOB WS connector both
    #: emit ``source="clob"``; the connector names differ).
    source: str = "base"

    #: Source type — REST / WebSocket / RSS / etc.
    source_type: str = "rest"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        pipeline_instance: Pipeline | None = None,
    ) -> None:
        self.config: dict[str, Any] = dict(config or {})
        self._pipeline = pipeline_instance or pipeline
        self._health = ConnectorHealth()
        self._lock = threading.Lock()
        # Register with the global registry so the observability layer
        # can find this connector (and so a future wave can iterate
        # every connector for a unified ``health_check`` endpoint).
        connector_registry.register(self)

    # ── Health ─────────────────────────────────────────────────────────

    def get_health(self) -> dict[str, Any]:
        """Return live connector health (JSON-serialisable)."""
        with self._lock:
            h = self._health.to_dict()
        return {
            "name": self.name,
            "source": self.source,
            "source_type": self.source_type,
            **h,
        }

    def reset_health(self) -> None:
        """Zero every counter (test-only)."""
        with self._lock:
            self._health.reset()

    def _record_success(self, latency_ms: float) -> None:
        with self._lock:
            self._health.requests_total += 1
            self._health.success_count += 1
            self._health.last_success_at = time.time()
            self._health.last_latency_ms = float(latency_ms)
            self._health.latencies_ms.append(float(latency_ms))

    def _record_error(self, err_msg: str, latency_ms: float = 0.0) -> None:
        with self._lock:
            self._health.requests_total += 1
            self._health.error_count += 1
            self._health.last_error_at = time.time()
            self._health.last_error_msg = str(err_msg)[:200]
            self._health.last_latency_ms = float(latency_ms)
            self._health.latencies_ms.append(float(latency_ms))

    # ── Pipeline dispatch ──────────────────────────────────────────────

    async def _push(
        self,
        source_id: str,
        event_type: str,
        raw_payload: Any,
        event_time: float | None = None,
    ) -> Any:
        """Push a record through the pipeline (offloaded to a worker
        thread so the sync validator doesn't block the event loop).

        Returns the ``PipelineResult`` (so a connector can branch on
        ``success``). Never raises — pipeline errors are recorded in
        the result's ``error_reason``.
        """
        # ``asyncio.to_thread`` runs the sync ``pipeline.process`` in a
        # thread-pool worker. The pipeline's own ``_lock`` guards the
        # counters; the underlying ``raw_vault`` uses SQLite's
        # ``BEGIN IMMEDIATE`` transaction for cross-thread serialisation.
        return await asyncio.to_thread(
            self._pipeline.process,
            self.source,
            source_id,
            event_type,
            raw_payload,
            event_time,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """Lightweight liveness probe. Subclasses override to actually
        call the upstream; the base implementation just returns the
        cached health dict.
        """
        return self.get_health()

    async def close(self) -> None:
        """Release any underlying resources (HTTP client / WS connection).
        Subclasses override. The base implementation is a no-op.
        """
        return None


# ── CLOB REST connector ───────────────────────────────────────────────────────


class ClobRestConnector(BaseConnector):
    """Polymarket CLOB REST API connector.

    Wraps ``core.clob_client.clob_client``. Each successful fetch is
    forwarded to the pipeline as a record (``order_book`` for
    ``/book`` responses, ``trade`` for ``/trades`` entries,
    ``snapshot`` for ``/last-trade-price`` / ``/price`` / ``/spread``).

    The underlying ``clob_client`` already integrates the W13-2
    ``clob_breaker`` circuit breaker and the W24-7 ``api_resilience``
    retry layer; this connector does NOT re-implement either. A
    circuit-open / retry-exhausted error is recorded in
    ``_record_error`` and the connector's health metrics reflect the
    degraded state (``error_count`` rises, ``last_error_msg`` carries
    the cause).
    """

    name = "clob_rest"
    source = "clob"
    source_type = "rest"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        pipeline_instance: Pipeline | None = None,
        client: Any | None = None,
    ) -> None:
        super().__init__(config=config, pipeline_instance=pipeline_instance)
        # ``client`` is optional so a test can inject a mock. The
        # production singleton is imported lazily (in ``_ensure_client``)
        # so this module doesn't force a PG / HTTP connection at
        # import time.
        self._client: Any = client

    async def _ensure_client(self) -> Any:
        if self._client is None:
            # Lazy import — mirrors the pattern in ``core/book_poller.py``
            # so this module imports cleanly even if the CLOB client
            # module hasn't been loaded yet.
            from core.clob_client import clob_client
            self._client = clob_client
        return self._client

    async def fetch_order_book(self, token_id: str) -> Any:
        """Fetch ``GET /book`` for a single token and forward as an
        ``order_book`` record. Returns the raw response (so a caller
        can branch on the book shape) — the pipeline has already
        received the record via ``_push``.
        """
        client = await self._ensure_client()
        start = time.time()
        try:
            data = await client.get_order_book(token_id)
        except Exception as e:
            self._record_error(str(e), (time.time() - start) * 1000.0)
            raise
        latency_ms = (time.time() - start) * 1000.0
        self._record_success(latency_ms)
        if data is not None:
            await self._push(
                source_id=f"book-{token_id}",
                event_type="order_book",
                raw_payload=data,
                event_time=float(data.get("timestamp") or 0.0) if isinstance(data, dict) else None,
            )
        return data

    async def fetch_public_trades(
        self,
        token_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch ``GET /trades`` (recent public tape) and forward each
        trade as an individual ``trade`` record. Returns the list of
        normalised trades (so a caller can iterate the same list the
        pipeline saw).
        """
        client = await self._ensure_client()
        start = time.time()
        try:
            trades = await client.get_public_trades(token_id=token_id, limit=limit)
        except Exception as e:
            self._record_error(str(e), (time.time() - start) * 1000.0)
            raise
        latency_ms = (time.time() - start) * 1000.0
        self._record_success(latency_ms)
        # Fan out — one ``process`` call per trade so each gets its
        # own dedup key + observation_id (a bulk record would
        # deduplicate every trade in the batch against the SAME key,
        # which would defeat the per-trade dedup contract).
        for t in trades:
            tid = str(t.get("trade_id") or t.get("id") or "")
            await self._push(
                source_id=tid or f"trade-{token_id or 'all'}-{t.get('timestamp', 0)}",
                event_type="trade",
                raw_payload=t,
                event_time=float(t.get("timestamp") or 0.0),
            )
        return trades

    async def fetch_last_trade_price(self, token_id: str) -> Any:
        """Fetch ``GET /last-trade-price`` and forward as a snapshot."""
        client = await self._ensure_client()
        start = time.time()
        try:
            data = await client.get_last_trade_price(token_id)
        except Exception as e:
            self._record_error(str(e), (time.time() - start) * 1000.0)
            raise
        latency_ms = (time.time() - start) * 1000.0
        self._record_success(latency_ms)
        if data is not None:
            await self._push(
                source_id=f"ltp-{token_id}",
                event_type="snapshot",
                raw_payload=data,
            )
        return data

    async def health_check(self) -> dict[str, Any]:
        """Call the underlying client's ``health_check`` and merge with
        the cached health metrics."""
        client = await self._ensure_client()
        start = time.time()
        try:
            latency_ms = await client.health_check()
        except Exception as e:
            self._record_error(str(e), (time.time() - start) * 1000.0)
            raise
        self._record_success(float(latency_ms))
        return self.get_health()


# ── Gamma REST connector ───────────────────────────────────────────────────────


class GammaRestConnector(BaseConnector):
    """Polymarket Gamma REST API connector.

    Wraps ``core.gamma_client.gamma_client``. Each successful fetch is
    forwarded as a ``market_info`` record (one per market in the
    paginated ``get_markets`` response, one per market in
    ``get_market``, one per event in ``get_events``).

    The underlying ``gamma_client`` already integrates the W13-2
    ``gamma_breaker`` circuit breaker and the W24-7 ``api_resilience``
    retry layer.
    """

    name = "gamma_rest"
    source = "gamma"
    source_type = "rest"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        pipeline_instance: Pipeline | None = None,
        client: Any | None = None,
    ) -> None:
        super().__init__(config=config, pipeline_instance=pipeline_instance)
        self._client: Any = client

    async def _ensure_client(self) -> Any:
        if self._client is None:
            from core.gamma_client import gamma_client
            self._client = gamma_client
        return self._client

    async def fetch_markets(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Fetch ``GET /markets`` and forward each market as a
        ``market_info`` record. Returns the list of markets.
        """
        client = await self._ensure_client()
        start = time.time()
        try:
            markets = await client.get_markets(
                active=active, closed=closed, limit=limit, offset=offset,
            )
        except Exception as e:
            self._record_error(str(e), (time.time() - start) * 1000.0)
            raise
        latency_ms = (time.time() - start) * 1000.0
        self._record_success(latency_ms)
        for m in markets:
            cid = str(m.get("condition_id") or m.get("id") or "")
            await self._push(
                source_id=cid or f"market-{offset}-{m.get('slug', '')}",
                event_type="market_info",
                raw_payload=m,
            )
        return markets

    async def fetch_market(self, condition_id: str) -> dict[str, Any]:
        """Fetch ``GET /markets/{condition_id}`` and forward as a
        ``market_info`` record.
        """
        client = await self._ensure_client()
        start = time.time()
        try:
            data = await client.get_market(condition_id)
        except Exception as e:
            self._record_error(str(e), (time.time() - start) * 1000.0)
            raise
        latency_ms = (time.time() - start) * 1000.0
        self._record_success(latency_ms)
        if data is not None:
            await self._push(
                source_id=condition_id,
                event_type="market_info",
                raw_payload=data,
            )
        return data

    async def fetch_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch ``GET /events`` and forward each event as a
        ``market_info`` record (events are the parent grouping of
        markets; the pipeline treats them uniformly with markets).
        """
        client = await self._ensure_client()
        start = time.time()
        try:
            events = await client.get_events(limit=limit)
        except Exception as e:
            self._record_error(str(e), (time.time() - start) * 1000.0)
            raise
        latency_ms = (time.time() - start) * 1000.0
        self._record_success(latency_ms)
        for ev in events:
            eid = str(ev.get("id") or ev.get("slug") or "")
            await self._push(
                source_id=eid,
                event_type="market_info",
                raw_payload=ev,
            )
        return events


# ── WebSocket connector ───────────────────────────────────────────────────────


class WebSocketConnector(BaseConnector):
    """Polymarket CLOB WebSocket connector.

    Wraps ``core.ws_client`` (the existing WS client singleton). The
    underlying client already handles reconnect / backoff / heartbeat;
    this connector hooks the inbound-message callback so every
    message is forwarded to the pipeline.

    A caller invokes ``start()`` (async, runs forever) to subscribe to
    a token list; the connector's ``_on_message`` callback dispatches
    each inbound message to ``_push``. The event_type is derived from
    the message's ``event_type`` field (``"book"`` → ``"order_book"``;
    ``"price_change"`` → ``"snapshot"``; ``"last_trade_price"`` →
    ``"trade"``).
    """

    name = "clob_ws"
    source = "clob"  # WS is the same source as REST — both emit "clob" records
    source_type = "websocket"

    #: Mapping from CLOB WS ``event_type`` strings to pipeline
    #: ``event_type`` strings. ``book`` → ``order_book`` is the only
    #: rename; everything else passes through.
    _EVENT_TYPE_MAP: dict[str, str] = {
        "book": "order_book",
        "price_change": "snapshot",
        "last_trade_price": "trade",
        "tick_price_change": "snapshot",
    }

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        pipeline_instance: Pipeline | None = None,
        client: Any | None = None,
    ) -> None:
        super().__init__(config=config, pipeline_instance=pipeline_instance)
        self._client: Any = client
        self._running: bool = False

    async def _ensure_client(self) -> Any:
        if self._client is None:
            # Lazy import so this module doesn't force a WS connection
            # at import time. The WS client singleton is constructed
            # without connecting — the connect happens in ``start()``.
            try:
                from core.ws_client import ws_client
                self._client = ws_client
            except ImportError:  # pragma: no cover — defensive
                self._client = None
        return self._client

    async def start(self, token_ids: list[str]) -> None:
        """Subscribe to ``token_ids`` and forward every inbound message
        to the pipeline. Runs until ``stop()`` is called (or the WS
        client's reconnect loop exhausts its retries).
        """
        client = await self._ensure_client()
        if client is None:
            self._record_error("ws_client not available")
            return
        self._running = True
        # Register the inbound-message callback BEFORE subscribing so
        # the first message after the subscribe ACK isn't dropped.
        # The callback is set as an attribute on the client (the
        # existing WS client supports an ``on_message`` hook).
        if hasattr(client, "on_message"):
            client.on_message = self._on_message
        try:
            if hasattr(client, "subscribe"):
                await client.subscribe(token_ids)
            elif hasattr(client, "connect"):
                await client.connect(token_ids)
        except Exception as e:
            self._record_error(str(e))
            raise

    async def stop(self) -> None:
        """Stop the connector's receive loop. The underlying WS client
        is left connected (the caller is responsible for closing it
        via ``close()``) so a quick ``stop`` + ``start`` cycle doesn't
        churn the connection.
        """
        self._running = False

    async def _on_message(self, msg: dict[str, Any]) -> None:
        """Inbound-message callback. Derives the event_type, source_id,
        and event_time from the message and forwards to the pipeline.
        """
        if not isinstance(msg, dict):
            return
        ws_event = str(msg.get("event_type") or msg.get("type") or "unknown")
        event_type = self._EVENT_TYPE_MAP.get(ws_event, ws_event)
        # ``asset_id`` / ``token_id`` / ``market`` — the WS message
        # shape varies by event_type.
        source_id = (
            str(msg.get("asset_id") or msg.get("token_id")
            or msg.get("market") or msg.get("condition_id") or "")
        )
        if not source_id:
            # No identifier in the message — synthesise one from the
            # event_type + timestamp so the dedup key is still unique
            # per (source, source_id, payload_hash).
            source_id = f"{ws_event}-{msg.get('timestamp', time.time())}"
        event_time = None
        ts = msg.get("timestamp") or msg.get("event_timestamp")
        if ts:
            try:
                event_time = float(ts)
            except (TypeError, ValueError):
                event_time = None
        await self._push(
            source_id=source_id,
            event_type=event_type,
            raw_payload=msg,
            event_time=event_time,
        )


# ── News / sentiment connector (stub) ─────────────────────────────────────────


class NewsSentimentConnector(BaseConnector):
    """News / sentiment feed connector (stub).

    No concrete upstream is wired yet — a future wave will plug a real
    RSS / Twitter / news-API source. The contract (``fetch_headlines``
    / ``fetch_sentiment``) is in place so a downstream consumer can
    be developed in parallel.

    The connector's ``fetch_*`` methods raise ``NotImplementedError``
    by default; a subclass (or a monkeypatch in a test) provides the
    real implementation. Health metrics still flow through the base
    class so the observability layer can render the connector's
    "configured but not yet wired" state.
    """

    name = "news_sentiment"
    source = "news"
    source_type = "rss"

    #: Default config — a subclass overrides via the constructor's
    #: ``config`` arg.
    DEFAULT_CONFIG: dict[str, Any] = {
        "headlines_url": "",
        "sentiment_url": "",
        "poll_interval_s": 300,
    }

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        pipeline_instance: Pipeline | None = None,
        fetcher: Callable[..., Any] | None = None,
    ) -> None:
        merged = {**self.DEFAULT_CONFIG, **(config or {})}
        super().__init__(config=merged, pipeline_instance=pipeline_instance)
        # ``fetcher`` is the callable that performs the actual HTTP
        # request. Production wires ``httpx.AsyncClient.get``; tests
        # inject a mock. ``None`` means "no upstream wired yet".
        self._fetcher = fetcher

    async def fetch_headlines(self, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """Fetch news headlines and forward each as a ``news`` record.

        Raises ``NotImplementedError`` if no ``fetcher`` is wired.
        """
        if self._fetcher is None:
            raise NotImplementedError(
                "NewsSentimentConnector.fetch_headlines requires a "
                "fetcher to be wired (constructor arg ``fetcher=``)."
            )
        start = time.time()
        try:
            headlines = await self._fetcher("headlines", query=query, limit=limit)
        except Exception as e:
            self._record_error(str(e), (time.time() - start) * 1000.0)
            raise
        latency_ms = (time.time() - start) * 1000.0
        self._record_success(latency_ms)
        for h in headlines or []:
            hid = str(h.get("id") or h.get("url") or h.get("title", "")[:50])
            await self._push(
                source_id=hid,
                event_type="news",
                raw_payload=h,
                event_time=float(h.get("published_at") or h.get("timestamp") or 0.0) or None,
            )
        return headlines or []

    async def fetch_sentiment(self, query: str = "") -> dict[str, Any]:
        """Fetch sentiment score for ``query`` and forward as a ``news``
        record (the sentiment payload IS the record — downstream
        consumers join it back to the headline by ``query``).
        """
        if self._fetcher is None:
            raise NotImplementedError(
                "NewsSentimentConnector.fetch_sentiment requires a "
                "fetcher to be wired (constructor arg ``fetcher=``)."
            )
        start = time.time()
        try:
            sentiment = await self._fetcher("sentiment", query=query)
        except Exception as e:
            self._record_error(str(e), (time.time() - start) * 1000.0)
            raise
        latency_ms = (time.time() - start) * 1000.0
        self._record_success(latency_ms)
        if sentiment is not None:
            await self._push(
                source_id=f"sentiment-{query or 'all'}",
                event_type="news",
                raw_payload=sentiment,
            )
        return sentiment


# ── Connector registry ────────────────────────────────────────────────────────


class ConnectorRegistry:
    """Tracks every instantiated connector for the observability layer.

    The registry is a simple ``dict[name → connector]``. Connectors
    register themselves in ``BaseConnector.__init__``; a caller can
    iterate the registry to render a single connector-health dashboard
    or run a unified ``health_check`` endpoint.
    """

    def __init__(self) -> None:
        self._connectors: dict[str, BaseConnector] = {}
        self._lock = threading.Lock()

    def register(self, connector: BaseConnector) -> None:
        with self._lock:
            # Last-write-wins — a re-instantiation of a connector with
            # the same ``name`` replaces the prior instance (the prior
            # instance's health metrics are dropped; the new instance
            # starts from zero). Mirrors the singleton semantics of
            # the W24-4 ``data_validator`` / W24-6 ``dedup_registry``.
            self._connectors[connector.name] = connector

    def unregister(self, name: str) -> None:
        with self._lock:
            self._connectors.pop(name, None)

    def get(self, name: str) -> BaseConnector | None:
        with self._lock:
            return self._connectors.get(name)

    def all(self) -> list[BaseConnector]:
        with self._lock:
            return list(self._connectors.values())

    def health_snapshot(self) -> list[dict[str, Any]]:
        """Return every connector's health dict (for the observability
        API)."""
        return [c.get_health() for c in self.all()]

    def clear(self) -> None:
        """Drop every registered connector (test-only)."""
        with self._lock:
            self._connectors.clear()


# ── Module-level singleton ────────────────────────────────────────────────────
connector_registry = ConnectorRegistry()


__all__ = [
    "BaseConnector",
    "ClobRestConnector",
    "GammaRestConnector",
    "WebSocketConnector",
    "NewsSentimentConnector",
    "ConnectorHealth",
    "ConnectorRegistry",
    "connector_registry",
]
