"""Unified broker interface for backtest/live parity.

God Mode §32 — Backtest/Live Parity
-------------------------------------
Prior to W19-7, the backtest engine
(``backtesting/engine.py::_SyntheticOrderBook.consume``) and the
paper/live broker (``paper/simulator.py::PaperSimulator._apply_slippage``)
implemented two completely incompatible slippage models:

  * The **backtest** engine modelled a synthetic CLOB-style book with
    ``spread_bps`` + ``depth_decay`` + a square-root market-impact term
    layered on top. The fill price walked through 5 levels of the
    synthetic book.
  * The **paper/live** simulator modelled three additive tick-based
    penalties (1-tick crossing, 0.5-tick per ``SLIPPAGE_DEPTH_BUCKET``
    shares of overflow, 0-or-1 deterministic queue tick from a stable
    SHA-256 hash of the ``order_id``).

Zero code was shared between the two paths. A strategy tested in
backtest would not see the same slippage shape in paper / live, so
backtest-vs-live fill parity was structurally impossible.

This module introduces a single ``Broker`` ABC + three concrete
implementations (``PaperBroker``, ``LiveBroker``, ``BacktestBroker``)
plus a ``get_broker(mode)`` factory. The three implementations share
``apply_slippage`` by delegating to the canonical
``paper.simulator.PaperSimulator._apply_slippage`` static method — that
is the ONE slippage model the system now uses everywhere. Strategies
that consume a ``Broker`` instance (rather than ``paper_sim`` /
``clob_client`` directly) get identical execution semantics across
backtest, paper, and live.

Adaptation notes
-----------------
The paper simulator's slippage static method is
``_apply_slippage(order: Order, raw_price: float, book: OrderBook) -> float``
(it returns a single slipped fill price; the ``Order`` supplies
``order_id`` (queue-tick source), ``side``, and ``size``). The
unified ``Broker.apply_slippage`` interface is the higher-level
``apply_slippage(price, size, side, order_book=None) -> (fill_price,
fill_size)``. Each implementation bridges the two by constructing a
minimal synthetic ``Order`` + ``OrderBook`` from the supplied scalars
when the caller doesn't already have one in hand. ``fill_size`` is
always the requested ``size`` — the paper simulator's slippage model
does not reduce fill size (it only shifts the price); a future
partial-fill-aware model can override ``apply_slippage`` per broker.

This module is **additive only**: it does not modify the existing
``paper.simulator``, ``core.clob_client``, or
``backtesting.engine`` modules. The legacy entrypoints (``paper_sim``,
``clob_client``, ``Backtester``) continue to work unchanged; the
``Broker`` interface is a new layer strategies may opt into.
"""
from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# ── Request / response dataclasses ───────────────────────────────────────────


@dataclass
class OrderRequest:
    """Broker-agnostic order request.

    Fields mirror the union of the paper simulator's ``OrderArgs`` and
    the live CLOB ``OrderArgs`` so a strategy can construct one
    ``OrderRequest`` and route it to any ``Broker`` subclass without
    per-mode field shuffling.
    """

    token_id: str
    side: str  # "BUY" or "SELL"
    size: float
    price: float
    order_type: str = "limit"  # "limit" or "market"
    time_in_force: str = "GTC"  # GTC, IOC, FOK
    client_order_id: str = ""
    strategy: str = ""
    decision_id: str = ""

    def __post_init__(self) -> None:
        if not self.client_order_id:
            # 12-char prefix mirrors the paper simulator's
            # ``paper-{uuid.uuid4().hex[:12]}`` convention so client-
            # minted IDs and broker-minted IDs are visually
            # distinguishable yet structurally compatible.
            self.client_order_id = str(uuid.uuid4())[:12]
        # Normalise side to upper-case string so callers may pass
        # "buy"/"sell" or a Side enum without per-casing boilerplate.
        self.side = str(self.side).upper()


@dataclass
class OrderResponse:
    """Broker-agnostic order response.

    Status values mirror the union of Polymarket CLOB statuses and the
    paper simulator's ``OrderStatus`` enum so the same set of strings
    flows through both paths. ``fill_price`` / ``fill_size`` are zero
    on acknowledgement and populated on FILLED / PARTIALLY_FILLED.
    """

    order_id: str
    status: str  # "ACKNOWLEDGED", "FILLED", "PARTIALLY_FILLED", "REJECTED", "CANCELLED"
    fill_price: float = 0.0
    fill_size: float = 0.0
    remaining_size: float = 0.0
    timestamp: float = field(default_factory=time.time)
    error: str = ""


@dataclass
class Position:
    """Broker-agnostic position snapshot.

    Mirrors a subset of ``core.data_store.Position`` (the fields a
    strategy is allowed to consume) without coupling the ``Broker``
    interface to the in-memory ``store`` singleton's dataclass.
    """

    token_id: str
    side: str  # "LONG" or "SHORT"
    size: float
    avg_price: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0


# ── Broker ABC ───────────────────────────────────────────────────────────────


class Broker(ABC):
    """Unified broker interface.

    Implementations:

      * ``BacktestBroker`` — Simulates fills from historical data.
        Holds its own capital + positions ledger so it is fully
        hermetic (no shared ``store`` singleton).
      * ``PaperBroker`` — Delegates to ``paper.simulator.paper_sim``
        for order submission + cancellation, applies the canonical
        tick-based slippage model.
      * ``LiveBroker`` — Delegates to ``core.clob_client.clob_client``
        for signed EIP-712 order submission to the Polymarket CLOB;
        uses the canonical paper-simulator slippage model for
        estimation (the live exchange charges real fees + spread, not
        the simulator's tick model — but pre-trade *estimation* needs
        one consistent model so the same strategy code can size
        positions identically across venues).

    Strategies that depend on a ``Broker`` instance — rather than the
    concrete ``paper_sim`` / ``clob_client`` singletons — get
    backtest/live parity for free: the slippage model is the same in
    every venue, and the order state transitions follow the same
    ``OrderRequest → OrderResponse`` contract.
    """

    @abstractmethod
    async def submit_order(self, request: OrderRequest) -> OrderResponse:
        """Submit an order and return the response.

        Paper / Live brokers return an ``ACKNOWLEDGED`` response with
        a server-minted ``order_id``; the fill lands asynchronously
        via the simulator's 1-second fill loop (paper) or the CLOB's
        WebSocket fill stream (live — once W18 fill-ack lands).

        ``BacktestBroker`` returns a synchronous ``FILLED`` /
        ``REJECTED`` response because there is no asynchronous
        execution venue in the backtest path.
        """
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order. Returns ``True`` if cancelled, ``False`` if
        the order was unknown or already terminal."""
        ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> Optional[OrderResponse]:
        """Get the current status of an order.

        Returns ``None`` if the order is unknown to this broker.
        """
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Get all open positions as broker-agnostic ``Position``
        snapshots."""
        ...

    @abstractmethod
    async def get_balance(self) -> float:
        """Get account balance (USDC available for new orders)."""
        ...

    @abstractmethod
    def apply_slippage(
        self,
        price: float,
        size: float,
        side: str,
        order_book: Optional[dict] = None,
    ) -> tuple[float, float]:
        """Apply the canonical slippage model to estimate fill price.

        Returns ``(fill_price, fill_size)``. The single canonical
        model lives on ``paper.simulator.PaperSimulator._apply_slippage``
        — every ``Broker`` subclass delegates to it so backtest,
        paper, and live share one slippage shape.

        ``order_book`` is an optional dict shaped like
        ``{"bids": [{"price": p, "size": s}, ...], "asks": [...]}``
        used to compute top-of-book depth for the size-impact term.
        When omitted, the broker synthesises a deep top-of-book so the
        size-impact term is zero (only the flat 1-tick crossing +
        queue ticks contribute).
        """
        ...

    # ── Shared helper: bridge to ``PaperSimulator._apply_slippage`` ──────────
    #
    # Concrete subclasses call this to delegate the slippage computation
    # to the canonical tick-based model without each one re-implementing
    # the order/book construction boilerplate. Marked as a staticmethod
    # (not abstractmethod) so it's a concrete utility, not part of the
    # ABC contract.

    @staticmethod
    def _canonical_slippage(
        price: float,
        size: float,
        side: str,
        order_book: Optional[dict] = None,
    ) -> tuple[float, float]:
        """Bridge ``(price, size, side, order_book)`` to the paper
        simulator's static ``_apply_slippage(order, raw_price, book)``.

        Constructs a minimal synthetic ``Order`` + ``OrderBook`` so the
        simulator's slippage logic runs unchanged. ``fill_size`` is
        the requested ``size`` (the canonical model only adjusts the
        fill price; size reduction is a future partial-fill extension).

        Local imports keep ``core.broker`` decoupled from
        ``paper.simulator`` + ``core.data_store`` at module-load time —
        the same lazy-import pattern used by ``core.execution_interface``
        and the paper simulator's own decision-ledger hook.
        """
        from core.data_store import Order, OrderBook, PriceLevel, Side
        from paper.simulator import PaperSimulator

        side_enum = Side.BUY if str(side).upper() == "BUY" else Side.SELL
        # Stable order_id derived from (price, size, side) so the
        # queue-tick hash is deterministic across calls with the same
        # arguments (mirrors the simulator's reproducibility contract).
        synthetic_id = f"broker-slippage-{side_enum.value}-{price:.6f}-{size:.6f}"
        synthetic_order = Order(
            order_id=synthetic_id,
            token_id="broker-slippage-token",
            side=side_enum,
            price=float(price),
            size=float(size),
            paper=True,
        )

        # Build a synthetic book from the caller-supplied dict, or fall
        # back to a deep top-of-book so the size-impact term is zero
        # (only the crossing + queue penalties contribute — keeps the
        # estimator conservative when the caller has no book snapshot).
        if order_book is not None and isinstance(order_book, dict):
            bids_raw = order_book.get("bids") or []
            asks_raw = order_book.get("asks") or []
            bids = [
                PriceLevel(price=float(lvl["price"]), size=float(lvl["size"]))
                for lvl in bids_raw
                if isinstance(lvl, dict) and "price" in lvl and "size" in lvl
            ]
            asks = [
                PriceLevel(price=float(lvl["price"]), size=float(lvl["size"]))
                for lvl in asks_raw
                if isinstance(lvl, dict) and "price" in lvl and "size" in lvl
            ]
            # If the caller supplied an empty side, synthesise a deep
            # level on that side so the simulator's
            # ``book.asks[0].size`` / ``book.bids[0].size`` lookup
            # doesn't raise IndexError (the simulator's slippage model
            # reads top-of-book depth unconditionally).
            if not bids:
                bids = [PriceLevel(price=max(0.01, float(price) - 0.01), size=1e9)]
            if not asks:
                asks = [PriceLevel(price=min(0.99, float(price) + 0.01), size=1e9)]
        else:
            # No caller-supplied book → deep top-of-book on both sides
            # centred on the requested price. The simulator reads
            # ``book.asks[0].size`` / ``book.bids[0].size`` to compute
            # overflow → size_impact_ticks; a 1e9-share top level means
            # overflow = 0 → size_impact_ticks = 0 (only the flat
            # 1-tick crossing + 0-or-1-tick queue contribute).
            bids = [PriceLevel(price=max(0.01, float(price) - 0.01), size=1e9)]
            asks = [PriceLevel(price=min(0.99, float(price) + 0.01), size=1e9)]

        synthetic_book = OrderBook(
            token_id="broker-slippage-token",
            bids=bids,
            asks=asks,
        )

        slipped = PaperSimulator._apply_slippage(
            synthetic_order, float(price), synthetic_book,
        )
        return (float(slipped), float(size))


# ── PaperBroker ──────────────────────────────────────────────────────────────


class PaperBroker(Broker):
    """Paper trading broker — uses ``paper.simulator.paper_sim``.

    Delegates ``submit_order`` / ``cancel_order`` to the paper
    simulator's existing async API (which records the local ``Order``,
    runs the 1-second fill loop, and records the ORDER / FILL stages
    in the decision ledger). ``apply_slippage`` delegates to the
    canonical ``PaperSimulator._apply_slippage`` static method.

    The ``store`` singleton is shared with the rest of the pipeline
    (positions / balance live on ``store``; ``paper_sim`` caches a
    mirror of ``store.paper_balance`` in ``_virtual_balance_usdc``
    so the simulator's fill loop can update it without holding the
    store lock).
    """

    def __init__(self) -> None:
        # Lazy import keeps ``core.broker`` importable in environments
        # where the paper-simulator singleton isn't yet constructed
        # (e.g. import-order tests).
        from paper.simulator import paper_sim
        self._sim = paper_sim

    async def submit_order(self, request: OrderRequest) -> OrderResponse:
        """Submit to the paper simulator; return an ACKNOWLEDGED
        response with the simulator-minted ``order_id``.

        The simulator's ``create_order`` returns the local ``Order``
        (with the canonical ``paper-<uuid>`` id) so we surface that
        id on the response. The actual fill lands asynchronously via
        the simulator's 1-second fill loop — strategies that need to
        wait for the fill should poll ``get_order_status``.
        """
        from core.clob_client import OrderArgs
        from core.data_store import Side

        args = OrderArgs(
            token_id=request.token_id,
            price=request.price,
            side=Side.BUY if request.side == "BUY" else Side.SELL,
            size=request.size,
        )
        try:
            order = await self._sim.create_order(
                args,
                strategy=request.strategy,
                decision_id=request.decision_id,
                order_id=request.client_order_id,
            )
        except Exception as exc:
            log.warning("[PaperBroker] submit_order raised: %s", exc)
            return OrderResponse(
                order_id=request.client_order_id,
                status="REJECTED",
                error=str(exc),
                timestamp=time.time(),
            )

        return OrderResponse(
            order_id=order.order_id,
            status="ACKNOWLEDGED",
            remaining_size=request.size,
            timestamp=time.time(),
        )

    async def cancel_order(self, order_id: str) -> bool:
        try:
            return await self._sim.cancel_order(order_id)
        except Exception as exc:
            log.debug("[PaperBroker] cancel_order raised: %s", exc)
            return False

    async def get_order_status(self, order_id: str) -> Optional[OrderResponse]:
        """Look up the order in the shared ``store.open_orders`` /
        ``store.order_history``. Returns ``None`` if the broker has no
        record of the order.

        Maps the in-memory ``Order`` dataclass to the broker-agnostic
        ``OrderResponse`` so consumers don't couple to ``core.data_store``.
        """
        from core.data_store import store

        order = store.open_orders.get(order_id)
        if order is None:
            for historic in store.order_history:
                if historic.order_id == order_id:
                    order = historic
                    break
        if order is None:
            return None

        status_map = {
            "OPEN": "ACKNOWLEDGED",
            "FILLED": "FILLED",
            "CANCELLED": "CANCELLED",
            "PARTIALLY_FILLED": "PARTIALLY_FILLED",
        }
        return OrderResponse(
            order_id=order.order_id,
            status=status_map.get(order.status.value, "ACKNOWLEDGED"),
            fill_size=order.size_matched,
            remaining_size=order.size_remaining,
            timestamp=order.created_at,
        )

    async def get_positions(self) -> list[Position]:
        """Map ``store.positions`` (``{token_id: Position}``) to
        broker-agnostic ``Position`` snapshots.

        Polymarket binary-outcome positions are YES-share longs in the
        current production code path (the simulator's _execute_fill
        only books YES-share entries — see paper/simulator.py:344),
        so every position with ``yes_shares > 0`` is reported as side
        ``LONG``; ``no_shares > 0`` would be ``SHORT`` (reserved for
        future NO-share entry support).
        """
        from core.data_store import store

        snapshots: list[Position] = []
        for token_id, pos in store.positions.items():
            if pos.yes_shares > 0:
                snapshots.append(Position(
                    token_id=token_id,
                    side="LONG",
                    size=pos.yes_shares,
                    avg_price=pos.avg_entry_price,
                    realized_pnl=pos.realised_pnl,
                ))
            elif pos.no_shares > 0:
                snapshots.append(Position(
                    token_id=token_id,
                    side="SHORT",
                    size=pos.no_shares,
                    avg_price=pos.avg_entry_price,
                    realized_pnl=pos.realised_pnl,
                ))
        return snapshots

    async def get_balance(self) -> float:
        """Return the simulator's virtual USDC balance (cached mirror
        of ``store.paper_balance``; re-synced on every fill)."""
        return float(self._sim.virtual_balance)

    def apply_slippage(
        self,
        price: float,
        size: float,
        side: str,
        order_book: Optional[dict] = None,
    ) -> tuple[float, float]:
        """Delegate to the canonical ``PaperSimulator._apply_slippage``
        via the shared ``Broker._canonical_slippage`` helper."""
        return Broker._canonical_slippage(price, size, side, order_book)


# ── LiveBroker ───────────────────────────────────────────────────────────────


class LiveBroker(Broker):
    """Live trading broker — uses ``core.clob_client.clob_client``.

    Delegates ``submit_order`` / ``cancel_order`` to the live CLOB
    REST client (EIP-712 signed orders POSTed to ``/order``). The
    ``apply_slippage`` estimator uses the same canonical paper-sim
    slippage model so a strategy sizing positions in live mode uses
    the same slippage shape it tested against in paper / backtest.
    """

    def __init__(self) -> None:
        from core.clob_client import clob_client
        self._clob = clob_client

    async def submit_order(self, request: OrderRequest) -> OrderResponse:
        """Sign and submit an EIP-712 order to the CLOB. Returns
        ``ACKNOWLEDGED`` on success (the fill lands asynchronously
        via the CLOB WebSocket fill stream — once W18 fill-ack lands).

        Returns ``REJECTED`` if the CLOB client returns ``None``
        (signing failure, HTTP 4xx/5xx, network error, missing
        credentials) — the simulator's create_order never returns
        ``None``, so a ``REJECTED`` response is unambiguous evidence
        of a live-submission failure.
        """
        from core.clob_client import OrderArgs
        from core.data_store import Side

        args = OrderArgs(
            token_id=request.token_id,
            price=request.price,
            side=Side.BUY if request.side == "BUY" else Side.SELL,
            size=request.size,
            order_type=request.time_in_force,
        )
        try:
            resp = await self._clob.create_order(args)
        except Exception as exc:
            log.warning("[LiveBroker] submit_order raised: %s", exc)
            return OrderResponse(
                order_id=request.client_order_id,
                status="REJECTED",
                error=str(exc),
                timestamp=time.time(),
            )

        if resp is None:
            return OrderResponse(
                order_id=request.client_order_id,
                status="REJECTED",
                error="CLOB submission returned None (signing / HTTP / network failure)",
                timestamp=time.time(),
            )

        order_id = (
            resp.get("orderID")
            or resp.get("order_id")
            or request.client_order_id
        )
        return OrderResponse(
            order_id=str(order_id),
            status="ACKNOWLEDGED",
            remaining_size=request.size,
            timestamp=time.time(),
        )

    async def cancel_order(self, order_id: str) -> bool:
        try:
            return await self._clob.cancel_order(order_id)
        except Exception as exc:
            log.debug("[LiveBroker] cancel_order raised: %s", exc)
            return False

    async def get_order_status(self, order_id: str) -> Optional[OrderResponse]:
        """Live order status lookup. The current ``clob_client`` does
        not expose a per-order ``GET /order/{id}`` endpoint (live fill
        ack is the W18 follow-up), so this returns ``None`` for now —
        a strategy that needs status polling should use the
        ``store.open_orders`` lookup path via ``PaperBroker`` /
        ``BacktestBroker`` until the W18 fill-ack lands.

        Returning ``None`` is safer than synthesising a stale response
        because the broker caller's contract is "``None`` ⇒ unknown
        to this broker" — a live order with no fill ack is genuinely
        unknown to the local state, even if it's resting on the
        exchange.
        """
        return None

    async def get_positions(self) -> list[Position]:
        """Fetch live positions from the CLOB ``GET /positions``
        endpoint and map to broker-agnostic ``Position`` snapshots.

        The CLOB returns each position as a dict with at least
        ``asset`` (the token id), ``size`` (signed; positive = long),
        and ``avgPrice`` (or ``avg_price``). We map positive size →
        ``LONG`` and negative size → ``SHORT`` (absolute value) so
        the broker-agnostic snapshot is direction-aware.
        """
        try:
            raw_positions = await self._clob.get_positions()
        except Exception as exc:
            log.warning("[LiveBroker] get_positions raised: %s", exc)
            return []

        if not isinstance(raw_positions, list):
            return []

        snapshots: list[Position] = []
        for raw in raw_positions:
            if not isinstance(raw, dict):
                continue
            token_id = (
                raw.get("asset")
                or raw.get("token_id")
                or raw.get("tokenId")
                or ""
            )
            if not token_id:
                continue
            try:
                size_signed = float(
                    raw.get("size") or raw.get("shares") or 0.0
                )
            except (TypeError, ValueError):
                size_signed = 0.0
            try:
                avg_px = float(
                    raw.get("avgPrice")
                    or raw.get("avg_price")
                    or raw.get("avgEntryPrice")
                    or 0.0
                )
            except (TypeError, ValueError):
                avg_px = 0.0
            if size_signed >= 0:
                snapshots.append(Position(
                    token_id=str(token_id),
                    side="LONG",
                    size=abs(size_signed),
                    avg_price=avg_px,
                ))
            else:
                snapshots.append(Position(
                    token_id=str(token_id),
                    side="SHORT",
                    size=abs(size_signed),
                    avg_price=avg_px,
                ))
        return snapshots

    async def get_balance(self) -> float:
        """Fetch live USDC balance from the CLOB
        ``GET /balance-allowance`` endpoint.

        The CLOB response shape is ``{"balance": "...", "allowance":
        "..."}`` (both as decimal strings). We parse ``balance`` as a
        float; on any failure we return ``0.0`` so the caller's
        capital check fails closed (no new orders submitted when we
        can't confirm the balance).
        """
        try:
            resp = await self._clob.get_balance()
        except Exception as exc:
            log.warning("[LiveBroker] get_balance raised: %s", exc)
            return 0.0
        if not isinstance(resp, dict):
            return 0.0
        try:
            return float(resp.get("balance", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def apply_slippage(
        self,
        price: float,
        size: float,
        side: str,
        order_book: Optional[dict] = None,
    ) -> tuple[float, float]:
        """Use the same canonical slippage model as paper / backtest
        for pre-trade estimation. The live exchange charges real
        fees + spread at fill time; the estimator's job is to give
        the strategy a consistent size signal across venues, not to
        predict the live fill price exactly."""
        return Broker._canonical_slippage(price, size, side, order_book)


# ── BacktestBroker ───────────────────────────────────────────────────────────


class BacktestBroker(Broker):
    """Backtest broker — simulates fills from historical data.

    Holds its OWN capital + positions ledger (no shared ``store``
    singleton) so backtests are fully hermetic: two BacktestBrokers
    running in the same process don't see each other's positions /
    balance. This is the structural fix for God Mode §32's
    "backtest-engine and paper/live broker share zero code" finding
    — by routing the backtest's order path through the same
    ``Broker`` interface the paper / live paths use, the slippage
    model is shared via ``apply_slippage``.

    Fills are immediate (no async fill loop) so the broker's
    ``submit_order`` returns a synchronous ``FILLED`` / ``REJECTED``
    response. ``cancel_order`` always returns ``False`` because
    there's nothing to cancel — the order already filled.
    """

    def __init__(self, initial_capital: float = 100.0) -> None:
        # Capital is the cash side of the ledger (USDC available for
        # new BUY orders). SELL proceeds credit back here. Matches
        # the ``BANKROLL_BASELINE`` ($100.00) default used by
        # ``core.data_store`` so a backtest starts from the same
        # capital base as a paper / live session.
        self._capital: float = float(initial_capital)
        # Positions are keyed by token_id; each value is the
        # broker-agnostic ``Position`` snapshot. We hold the snapshot
        # directly (rather than the ``core.data_store.Position``
        # dataclass) so the BacktestBroker has zero coupling to the
        # in-memory ``store`` singleton — the entire point of the
        # §32 parity fix.
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, OrderResponse] = {}

    async def submit_order(self, request: OrderRequest) -> OrderResponse:
        """Simulate an immediate fill at the requested price (or with
        canonical slippage applied via ``apply_slippage``).

        BUY: cost = fill_price * fill_size; reject if cost > capital.
        SELL: reject if no position; otherwise reduce position by
        ``fill_size`` (clamped to the open size) and credit the
        proceeds to capital; record realized P&L on the position
        snapshot.

        Slippage is applied via the canonical
        ``PaperSimulator._apply_slippage`` model (delegated through
        ``apply_slippage``) so a backtest sees the same slippage shape
        the paper / live brokers see — God Mode §32's parity contract.
        """
        # Apply the canonical slippage model so the backtest's fill
        # price reflects the same tick-based penalties (crossing + size
        # impact + queue) as paper / live. ``order_book`` is None
        # because the backtest broker doesn't carry a per-step book
        # snapshot — the size-impact term collapses to zero (only the
        # flat 1-tick crossing + 0-or-1-tick queue contribute), which
        # is the conservative estimator behaviour.
        fill_price, fill_size = self.apply_slippage(
            request.price, request.size, request.side, order_book=None,
        )

        if request.side == "BUY":
            cost = fill_price * fill_size
            if cost > self._capital + 1e-9:
                return OrderResponse(
                    order_id=request.client_order_id,
                    status="REJECTED",
                    error=(
                        f"Insufficient capital: cost ${cost:.4f} > "
                        f"balance ${self._capital:.4f}"
                    ),
                    timestamp=time.time(),
                )
            self._capital -= cost
            # Update / create the long position with weighted-average
            # entry price (mirrors ``core.data_store.Position``
            # accounting so a backtest's reported P&L matches the
            # paper / live accounting shape).
            if request.token_id in self._positions:
                pos = self._positions[request.token_id]
                new_size = pos.size + fill_size
                pos.avg_price = (
                    (pos.avg_price * pos.size) + (fill_price * fill_size)
                ) / new_size if new_size > 0 else fill_price
                pos.size = new_size
            else:
                self._positions[request.token_id] = Position(
                    token_id=request.token_id,
                    side="LONG",
                    size=fill_size,
                    avg_price=fill_price,
                )
        else:  # SELL
            pos = self._positions.get(request.token_id)
            if pos is None or pos.size <= 1e-9:
                return OrderResponse(
                    order_id=request.client_order_id,
                    status="REJECTED",
                    error=f"No position to sell for token {request.token_id}",
                    timestamp=time.time(),
                )
            # Clamp fill_size to the open position size so a SELL
            # larger than the position doesn't go negative.
            if fill_size > pos.size:
                fill_size = pos.size
            proceeds = fill_price * fill_size
            self._capital += proceeds
            # Realized P&L = (exit - entry) * shares_sold. Mirrors
            # paper/simulator.py:344-345 so backtest P&L attribution
            # matches paper / live attribution.
            pnl = (fill_price - pos.avg_price) * fill_size
            pos.size -= fill_size
            pos.realized_pnl += pnl
            if pos.size <= 1e-9:
                # Position fully closed — evict so get_positions()
                # doesn't report zero-size stubs.
                del self._positions[request.token_id]

        response = OrderResponse(
            order_id=request.client_order_id,
            status="FILLED",
            fill_price=fill_price,
            fill_size=fill_size,
            remaining_size=0.0,
            timestamp=time.time(),
        )
        self._orders[request.client_order_id] = response
        return response

    async def cancel_order(self, order_id: str) -> bool:
        """Backtest fills are synchronous — there is nothing to
        cancel. Returns ``False`` to signal the caller that the
        cancel didn't take effect (the order already filled)."""
        return False

    async def get_order_status(self, order_id: str) -> Optional[OrderResponse]:
        return self._orders.get(order_id)

    async def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def get_balance(self) -> float:
        return self._capital

    def apply_slippage(
        self,
        price: float,
        size: float,
        side: str,
        order_book: Optional[dict] = None,
    ) -> tuple[float, float]:
        """Use the canonical paper simulator slippage model for
        consistency with the paper / live brokers. This is the
        load-bearing fix for God Mode §32: before W19-7, the
        backtest engine walked a synthetic 5-level book with
        ``spread_bps`` + ``depth_decay`` + square-root market impact,
        while paper / live used tick-based crossing + size + queue.
        After W19-7, both paths route through this method."""
        return Broker._canonical_slippage(price, size, side, order_book)


# ── Factory ──────────────────────────────────────────────────────────────────


def get_broker(mode: str = "paper", **kwargs) -> Broker:
    """Factory to get the appropriate broker for a trading mode.

    Parameters
    ----------
    mode
        ``"paper"`` → ``PaperBroker`` (delegates to ``paper_sim``).
        ``"live"``  → ``LiveBroker`` (delegates to ``clob_client``).
        ``"backtest"`` → ``BacktestBroker`` (hermetic local ledger).
    **kwargs
        Passed through to the broker constructor. Currently only
        ``BacktestBroker`` consumes one — ``initial_capital`` (default
        ``100.0``). ``PaperBroker`` / ``LiveBroker`` ignore extra
        kwargs (they have no configurable constructor args).

    Returns
    -------
    Broker
        A concrete ``Broker`` instance for the requested mode.

    Raises
    ------
    ValueError
        If ``mode`` is not one of ``"paper"`` / ``"live"`` /
        ``"backtest"``.
    """
    if mode == "paper":
        return PaperBroker()
    if mode == "live":
        return LiveBroker()
    if mode == "backtest":
        initial_capital = float(kwargs.get("initial_capital", 100.0))
        return BacktestBroker(initial_capital=initial_capital)
    raise ValueError(
        f"Unknown broker mode: {mode!r} (expected 'paper', 'live', or 'backtest')"
    )


__all__ = [
    "Broker",
    "BacktestBroker",
    "LiveBroker",
    "PaperBroker",
    "OrderRequest",
    "OrderResponse",
    "Position",
    "get_broker",
]
