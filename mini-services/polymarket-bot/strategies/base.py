"""
strategies/base.py — Abstract base class for all trading strategies.

W19-2 — Unified Strategy Contract (God Mode §26).

This module now exposes the 9-method ``StrategyContract`` ABC that every
strategy in the bot must implement. ``BaseStrategy`` inherits from
``StrategyContract`` and provides default implementations so legacy
subclasses (and the 47 stub ``QuantStrategyInstance`` entries in
``strategies/registry.py``) keep working unchanged — only ``_run`` remains
strictly abstract.

The 9 contract methods (all SYNC — no async required):

  1. ``metadata()``                  → name / version / description / author
  2. ``configure(config)``           → apply a parameter dict post-construction
  3. ``validate()``                  → (is_valid, error_message)
  4. ``generate_signal(market_ctx)`` → ``Signal`` | ``None``
  5. ``estimate_edge(signal)``      → expected P&L per dollar
  6. ``size_position(signal, capital, risk_params)`` → position size (USDC)
  7. ``entry_logic(signal, market_ctx)``   → entry execution params dict
  8. ``exit_logic(position, market_ctx)``  → exit params dict | ``None``
  9. ``diagnostics()``               → state / stats / health / last_error

The contract is intentionally minimal: it does NOT prescribe how a strategy
runs its async loop (``_run``), how it submits orders (``submit_order``),
or how it cancels them (``cancel_order``). Those concerns remain on
``BaseStrategy`` unchanged so the existing live trading pipeline keeps
working. The contract only standardizes the *signal-generation → sizing →
execution-decision → diagnostics* surface that operators, dashboards, and
backtest engines need to introspect any strategy uniformly.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from config import settings
from core.clob_client import OrderArgs, clob_client
from core.data_store import Order, store
from paper.simulator import paper_sim
from risk.manager import risk_manager

log = logging.getLogger(__name__)


# ── W19-2 — Unified Signal value type (God Mode §26) ─────────────────────────
@dataclass
class Signal:
    """Standard signal output from ``generate_signal()``.

    Every strategy returns a ``Signal`` (or ``None`` when no actionable
    opportunity exists). The fields are deliberately permissive: a market
    maker can populate ``action="BUY"`` / ``action="SELL"`` for the two
    sides of a quote; an arbitrage scanner can encode a paired leg
    structure in ``metadata``; an ML directional trader can record its
    ``confidence`` and ``edge`` for downstream sizing / ledger linkage.

    ``metadata`` is a plain ``dict`` (default ``{}``) so callers can attach
    strategy-specific context (e.g. ``{"kelly_f": 0.15}``,
    ``{"no_token_id": "0x…"}``, ``{"arb_type": "long_dutch_book"}``)
    without subclassing.
    """

    action: str  # "BUY", "SELL", "HOLD"
    token_id: str
    size: float = 0.0
    price: Optional[float] = None
    confidence: float = 0.0
    edge: float = 0.0
    reason: str = ""
    metadata: dict = field(default_factory=dict)


# ── W19-2 — StrategyContract ABC (God Mode §26) ──────────────────────────────
class StrategyContract(ABC):
    """Unified strategy interface (God Mode §26).

    Every strategy — whether it's a market maker, an arbitrage scanner,
    an ML directional trader, or a stub catalog entry — must implement
    these 9 methods so that operators, dashboards, and backtest engines
    can introspect any strategy uniformly without knowing its concrete
    class.

    The contract is intentionally SYNC: no method here is a coroutine.
    Strategies that need to consult async sources (book poller, decision
    ledger, CLOB client) must do so out-of-band (e.g. inside their
    ``_run`` loop) and surface the resulting state through the contract
    methods as plain data. This keeps the contract safe to call from
    sync contexts — FastAPI request handlers, REPL introspection,
    backtest replay — without spinning up an event loop.

    ``BaseStrategy`` provides default implementations for every method
    (see below); concrete strategies override the ones they care about.
    """

    @abstractmethod
    def metadata(self) -> dict:
        """Return strategy metadata: name, version, description, author."""
        ...

    @abstractmethod
    def configure(self, config: dict) -> None:
        """Configure strategy parameters post-construction."""
        ...

    @abstractmethod
    def validate(self) -> tuple[bool, str]:
        """Validate strategy configuration. Returns (is_valid, error_message)."""
        ...

    @abstractmethod
    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        """Analyze market data and generate a trading signal (or ``None``)."""
        ...

    @abstractmethod
    def estimate_edge(self, signal: Signal) -> float:
        """Estimate the expected edge for a signal (expected P&L per dollar)."""
        ...

    @abstractmethod
    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        """Determine position size given signal, capital, and risk constraints."""
        ...

    @abstractmethod
    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        """Determine entry execution parameters (price, order type, etc.)."""
        ...

    @abstractmethod
    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Determine if/when to exit a position. Returns exit params or ``None``."""
        ...

    @abstractmethod
    def diagnostics(self) -> dict:
        """Return diagnostic info: state, stats, health, last_error."""
        ...


class BaseStrategy(StrategyContract):
    """
    All strategies inherit from this. Provides:

    - Unified order submission (paper or live)
    - Risk check integration
    - Lifecycle management (start/stop)
    - W19-2 — default implementations of the 9-method ``StrategyContract``
      so subclasses only need to override the contract methods they care
      about; everything else falls back to a sensible neutral default.

    Backward compatibility
    ----------------------
    The constructor signature is ``__init__(self, name: str = None,
    config: dict = None)`` so existing subclasses that call
    ``super().__init__()`` with no arguments keep working unchanged
    (``SignalTraderStrategy``, ``MarketMakerStrategy``,
    ``ArbScannerStrategy``, ``QuantStrategyInstance``). When ``name`` is
    omitted, the class attribute (``name = "signal_trader"``, etc.) is
    preserved — matching the pre-W19-2 behaviour exactly.
    """

    name: str = "base"

    def __init__(self, name: str = None, config: dict = None) -> None:
        # ``name`` overrides the class attribute when explicitly supplied
        # (the contract spec calls for ``self.name = name``); when omitted,
        # the existing class-attribute default (e.g. ``"signal_trader"``)
        # is preserved so the three real strategies and the 47 stub
        # ``QuantStrategyInstance`` entries keep working unchanged.
        if name is not None:
            self.name = name
        # W19-2 — strategy configuration dict. ``BaseStrategy`` keeps this
        # as a plain mutable dict so ``configure()`` can ``update()`` it
        # post-construction; subclasses can read typed fields off it
        # (``self.config.get("min_confidence", self._min_confidence)``).
        self.config: dict = config or {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._paper = settings.paper_trade
        # W19-2 — diagnostics counters surfaced through ``diagnostics()``.
        # Concrete strategies increment these as they generate signals /
        # place trades / hit errors; the base class only initializes the
        # shape so ``diagnostics()`` never raises ``KeyError`` on a fresh
        # instance.
        # W22-7 - extended with evaluations + rejects counters for the
        # canonical strategy.evaluations / strategy.rejects observability
        # metrics (God Mode §54). Default 0 - concrete strategies
        # increment them in their _run loops.
        self._stats: dict[str, int] = {
            "signals": 0,
            "trades": 0,
            "errors": 0,
            "evaluations": 0,
            "rejects": 0,
        }
        # W19-2 — last error message (if any) for the diagnostics surface.
        # ``None`` means "no error recorded"; strategies set this when they
        # catch an unexpected exception inside ``_run`` so the operator
        # dashboard can surface it without grepping logs.
        self._last_error: Optional[str] = None

    # ── W19-2 — StrategyContract default implementations ──────────────────────

    def metadata(self) -> dict:
        """Default: name + version + a generic description.

        Concrete strategies override this to surface their actual mission
        (e.g. ``"ML-Powered Directional Signal Trader with Kelly Sizing"``)
        and author / source URL / license info.
        """
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": "Base strategy — override metadata() in subclass",
            "author": "polymarket-bot",
        }

    def configure(self, config: dict) -> None:
        """Default: shallow-merge the supplied config dict into ``self.config``.

        Subclasses that need to re-derive typed fields (e.g.
        ``self._min_confidence = config.get("min_confidence",
        self._min_confidence)``) override this and call
        ``super().configure(config)`` first to preserve the dict.
        """
        if config:
            self.config.update(config)

    def validate(self) -> tuple[bool, str]:
        """Default: always valid. Override to enforce strategy-specific invariants."""
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        """Default: never generate a signal. Override in subclasses.

        The base implementation returns ``None`` so a freshly-constructed
        strategy (or a stub catalog entry from ``QuantStrategyInstance``)
        never accidentally fires a trade just because the contract method
        exists. Concrete strategies read ``market_context`` (token_id,
        order book snapshot, ML prediction, etc.) and return a populated
        ``Signal`` only when an actionable opportunity exists.
        """
        return None

    def estimate_edge(self, signal: Signal) -> float:
        """Default: return the signal's pre-computed ``edge`` field.

        ``Signal.edge`` is set by ``generate_signal`` when the strategy
        has enough information to estimate the expected P&L per dollar
        (e.g. the Kelly numerator for an ML directional trader, or
        ``1 - (ask_yes + ask_no)`` for a long Dutch-book arb). The
        default impl just surfaces that pre-computed value so callers
        don't need to know each strategy's edge formula.

        Returns ``0.0`` when ``signal is None`` so callers can chain
        ``edge = strat.estimate_edge(strat.generate_signal(ctx))``
        without a None-guard.
        """
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        """Default: 1% of capital.

        The 1% default is a deliberately conservative baseline — it
        ensures a misconfigured strategy (one that forgot to override
        ``size_position``) cannot blow up the bankroll on a single
        trade. Concrete strategies override with Kelly / A-S inventory-
        aware / fixed-notional sizing as appropriate.

        ``risk_params`` is an opaque dict; the default impl ignores it
        but concrete strategies read typed fields off it (e.g.
        ``risk_params.get("max_position_per_market", 3.0)``).
        """
        if signal is None or signal.action == "HOLD":
            return 0.0
        # Conservative 1% baseline; subclasses override.
        return capital * 0.01

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        """Default: limit order at the signal's price (or market mid).

        The default returns a plain dict so the contract return type
        stays JSON-serializable for the dashboard / API surface.
        Concrete strategies add fields as needed (``"time_in_force"``,
        ``"order_type"``, ``"post_only"`` …).
        """
        return {
            "price": signal.price if signal and signal.price is not None
            else market_context.get("mid", 0.5),
            "type": "limit",
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Default: never exit. Override in subclasses.

        Returning ``None`` means "no exit decision" — the position is
        left open and the caller (the ``_run`` loop, a dashboard, an
        external trade manager) is responsible for polling again later.
        Concrete strategies override to encode their exit rules
        (stop-loss, take-profit, time-based flush, etc.).
        """
        return None

    def diagnostics(self) -> dict:
        """Default: name + running flag + stats counters + last error.

        Concrete strategies override to add strategy-specific state
        (e.g. number of active quotes, number of pairs scanned,
        model readiness) but should call ``super().diagnostics()`` and
        ``.update()`` the result so the base fields are always present.
        """
        return {
            "name": self.name,
            "running": self._running,
            "stats": dict(self._stats),
            "last_error": self._last_error,
        }

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name=f"strategy-{self.name}")
        log.info("Strategy [%s] started (paper=%s)", self.name, self._paper)
        await store.log_event(f"▶ Strategy [{self.name}] started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Strategy [%s] stopped", self.name)
        await store.log_event(f"⏹ Strategy [{self.name}] stopped")

    @abstractmethod
    async def _run(self) -> None:
        """Main strategy loop. Override in subclasses."""
        ...

    # ── Order helpers ─────────────────────────────────────────────────────

    async def submit_order(self, args: OrderArgs, decision_id: str = "") -> Order | None:
        """
        Submit an order through the risk manager, then either:
        - Paper mode: record in PaperSimulator
        - Live mode: send to CLOB API
        Returns the Order object or None if rejected.

        ``decision_id`` (R11) links the resulting ORDER (and downstream FILL)
        stage to the originating PREDICTION → SIGNAL chain in the unified
        decision ledger. Defaults to "" for legacy / manual callers.

        W18-1 — Order State Machine (OSM) wiring (closes the P0-C-01 gap
        surfaced by the W17-2 Bot Execution Engine Assessment). Every
        submit-order path now records the canonical lifecycle
        (CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED → OPEN) in the
        SQLite-backed OSM audit trail before / after the actual exchange
        (or paper sim) call. The OSM calls are ADDITIVE: wrapped in
        ``try/except`` so a persistence failure never breaks trading
        (mirrors the fail-soft contract of every other audit singleton
        in the codebase).

        Identity linking: for paper orders the canonical ``order_id``
        is pre-minted in this method and passed to ``paper_sim.create_order``
        so the OSM audit trail and the in-memory ``Order`` share one
        identity. For live orders the exchange assigns its own order_id
        (returned in the CLOB response); we pre-mint an OSM tracking id
        (``ord-...``) and stamp the exchange-assigned id into the OSM
        entry's ``metadata.exchange_order_id`` field so the two can be
        cross-referenced from the audit trail.
        """
        # W19-2 — bump the trades counter on success, errors on failure
        # (best-effort: the OSM / ledger / risk paths already swallow
        # their own exceptions, so the only way this counter doesn't
        # increment is if the call returns ``None`` for a non-error
        # reason like risk rejection — that's a "no-op" not an "error",
        # so we don't bump errors there either).
        # W18-1 — pre-mint the canonical OSM tracking id so the OSM entry
        # and the production Order share one identity (paper path) or are
        # cross-referenceable (live path via metadata).
        side_str = (
            args.side.value if hasattr(args.side, "value") else str(args.side)
        )
        if self._paper:
            osm_order_id = f"paper-{uuid.uuid4().hex[:12]}"
        else:
            osm_order_id = f"ord-{uuid.uuid4().hex}"

        osm_order = None
        try:
            from core.order_state_machine import OrderState, osm
            osm_order = osm.create_order(
                strategy=self.name,
                token_id=args.token_id,
                side=side_str,
                price=args.price,
                size=args.size,
                decision_id=decision_id,
                order_id=osm_order_id,
                metadata={"paper": self._paper},
            )
        except Exception as e:
            log.debug(
                "[%s] OSM create_order failed (continuing without audit "
                "trail): %s", self.name, e,
            )

        # Build a provisional Order for risk checking
        provisional = Order(
            order_id=osm_order_id,
            token_id=args.token_id,
            side=args.side,
            price=args.price,
            size=args.size,
            strategy=self.name,
            paper=self._paper,
            decision_id=decision_id,
        )

        allowed, reason = await risk_manager.check_order(provisional)
        if not allowed:
            log.debug("[%s] Order blocked by risk: %s", self.name, reason)
            await store.log_event(f"⚠ Risk block [{self.name}]: {reason}")
            # R11 — record the RISK_REJECTED stage so the decision chain shows
            # why the order never reached the book.
            try:
                from core.decision_ledger import decision_ledger
                await decision_ledger.record(
                    decision_id=decision_id,
                    stage="RISK_REJECTED",
                    token_id=args.token_id,
                    strategy=self.name,
                    pnl=0.0,
                    side=side_str,
                    price=args.price,
                    size=args.size,
                    reason=reason,
                )
            except Exception as e:
                log.debug("[%s] ledger RISK_REJECTED record failed: %s", self.name, e)
            # W18-1 — record the RISK_REJECTED transition so a rejected
            # order is visible in the OSM audit trail with the rejection
            # reason stamped in metadata. Pass the order_id str (not the
            # Order instance) so the latest snapshot is loaded — protects
            # against the stale-Order-instance race (the local
            # ``osm_order`` var still reflects the CREATED state from
            # ``create_order``; the persisted snapshot is the canonical
            # truth source).
            try:
                from core.order_state_machine import OrderState, osm
                if osm_order is not None:
                    osm.transition(
                        osm_order.order_id,
                        OrderState.VALIDATED,
                        metadata={"risk_rejected": True, "reason": reason},
                    )
                    osm.transition(
                        osm_order.order_id,
                        OrderState.REJECTED,
                        metadata={"reason": reason},
                    )
            except Exception as e:
                log.debug(
                    "[%s] OSM RISK_REJECTED transition failed: %s",
                    self.name, e,
                )
            return None

        # R11 — record RISK_APPROVED before the order leaves the strategy layer
        # (paper or live), so a missing downstream ORDER/FILL stage surfaces as
        # a gap rather than a silent no-op.
        try:
            from core.decision_ledger import decision_ledger
            await decision_ledger.record(
                decision_id=decision_id,
                stage="RISK_APPROVED",
                token_id=args.token_id,
                strategy=self.name,
                pnl=0.0,
                side=side_str,
                price=args.price,
                size=args.size,
            )
        except Exception as e:
            log.debug("[%s] ledger RISK_APPROVED record failed: %s", self.name, e)

        # W18-1 — risk check passed; transition the OSM entry to VALIDATED
        # (the SUBMITTED → ACKNOWLEDGED → OPEN hops are recorded by the
        # paper / live submission paths below so each mode controls its
        # own submission semantics). Pass the order_id str (not the Order
        # instance) so the latest snapshot is loaded.
        try:
            from core.order_state_machine import OrderState, osm
            if osm_order is not None:
                osm.transition(osm_order.order_id, OrderState.VALIDATED)
        except Exception as e:
            log.debug(
                "[%s] OSM VALIDATED transition failed: %s", self.name, e,
            )

        # W23-2 — record the order-submission timestamp against the latency
        # tracker so the signal→order→fill pipeline latency is measurable
        # per correlation_id. ``decision_id`` is the same identifier the
        # decision ledger threads through the chain (alias for the
        # tracker's ``correlation_id``). Placed AFTER risk approval and
        # BEFORE the actual paper/live submit call so ``order_time`` is
        # anchored to "the moment the order was about to leave the
        # strategy layer" (the latency segment we care about for SLO
        # monitoring is signal→order, not signal→exchange-ack). Best-
        # effort: a tracker exception must NEVER block order submission.
        try:
            from core.latency_tracker import latency_tracker
            latency_tracker.record_order(correlation_id=decision_id)
        except Exception as e:
            log.debug(
                "[%s] latency_tracker.record_order failed: %s", self.name, e,
            )

        if self._paper:
            # W18-1 — pass the pre-minted ``osm_order_id`` so the OSM
            # entry and the in-memory Order share one identity.
            # ``paper_sim.create_order`` records the SUBMITTED →
            # ACKNOWLEDGED → OPEN hops on the same call.
            result = await paper_sim.create_order(
                args,
                strategy=self.name,
                decision_id=decision_id,
                order_id=osm_order_id,
            )
            # W19-2 — increment trades counter on successful paper submit.
            if result is not None:
                self._stats["trades"] = self._stats.get("trades", 0) + 1
            return result
        else:
            resp = await clob_client.create_order(args)
            if resp is None:
                # W18-1 — exchange rejected the order (signing failure,
                # HTTP error, exception inside the client). Record the
                # SUBMITTED → REJECTED hops so the audit trail shows the
                # order was attempted but rejected at the exchange.
                try:
                    from core.order_state_machine import OrderState, osm
                    if osm_order is not None:
                        osm.transition(osm_order.order_id, OrderState.SUBMITTED)
                        osm.transition(
                            osm_order.order_id,
                            OrderState.REJECTED,
                            metadata={"reason": "clob_client returned None"},
                        )
                except Exception as e:
                    log.debug(
                        "[%s] OSM REJECTED transition failed: %s",
                        self.name, e,
                    )
                # W19-2 — record the failure for diagnostics.
                self._stats["errors"] = self._stats.get("errors", 0) + 1
                self._last_error = "clob_client.create_order returned None"
                return None
            order_id = resp.get("orderID") or resp.get("order_id", "unknown")
            order = Order(
                order_id=order_id,
                token_id=args.token_id,
                side=args.side,
                price=args.price,
                size=args.size,
                strategy=self.name,
                paper=False,
                decision_id=decision_id,
            )
            await store.add_order(order)
            # W18-1 — record the SUBMITTED → ACKNOWLEDGED → OPEN hops so
            # the live order shows up as OPEN in the OSM audit trail. The
            # exchange-assigned ``order_id`` is stamped into the OSM
            # entry's ``metadata.exchange_order_id`` so the two ids can be
            # cross-referenced from the audit trail. Pass the
            # order_id str (not the Order instance) so the latest
            # snapshot is loaded on each hop.
            try:
                from core.order_state_machine import OrderState, osm
                if osm_order is not None:
                    osm.transition(
                        osm_order.order_id,
                        OrderState.SUBMITTED,
                        metadata={"exchange_order_id": order_id},
                    )
                    osm.transition(osm_order.order_id, OrderState.ACKNOWLEDGED)
                    osm.transition(osm_order.order_id, OrderState.OPEN)
            except Exception as e:
                log.debug(
                    "[%s] OSM live OPEN transitions failed: %s",
                    self.name, e,
                )
            # W19-2 — increment trades counter on successful live submit.
            self._stats["trades"] = self._stats.get("trades", 0) + 1
            return order

    async def cancel_order(self, order_id: str) -> bool:
        if self._paper:
            return await paper_sim.cancel_order(order_id)
        else:
            ok = await clob_client.cancel_order(order_id)
            # W18-1 — record the CANCELLED transition in the OSM audit
            # trail after a successful live cancel. Best-effort: a state-
            # machine failure (e.g. the OSM entry doesn't exist for a
            # legacy order, or the order was already terminal) is logged
            # and swallowed so a cancel never rolls back.
            if ok:
                try:
                    from core.order_state_machine import OrderState, osm
                    osm.transition(order_id, OrderState.CANCELLED)
                except Exception as e:
                    log.debug(
                        "OSM CANCELLED transition failed for %s: %s",
                        order_id, e,
                    )
            return ok
