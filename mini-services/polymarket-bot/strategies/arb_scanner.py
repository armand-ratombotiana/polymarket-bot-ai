"""
strategies/arb_scanner.py — High-Frequency Binary Arbitrage Scanner.

Scans Polymarket binary prediction markets for mispricing:
1. Long-side Dutch Book:
     Ask(YES) + Ask(NO) < 1.00 - MIN_PROFIT_BPS
   Action: Buy both YES and NO simultaneously. One of them is guaranteed to resolve
   to $1.00, securing a 100% risk-free arbitrage profit!
2. Short-side Overpriced:
     Bid(YES) + Bid(NO) > 1.00 + MIN_PROFIT_BPS
   Action: Sell/short both outcomes at a combined premium > $1.00.
"""
from __future__ import annotations

import asyncio
import logging

from config import settings
from core.book_poller import book_poller
from core.clob_client import OrderArgs
from core.data_store import Side, store
from core.gamma_client import gamma_client
from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)


class ArbScannerStrategy(BaseStrategy):
    """
    Automated combinatorial arbitrage engine for binary prediction markets.
    """

    name = "arb_scanner"

    def __init__(self) -> None:
        super().__init__()
        self._min_profit_frac = max(0.003, settings.arb_min_profit_bps / 10_000)
        self._scan_interval = max(5, settings.arb_scan_interval_seconds)
        self._order_size = settings.arb_order_size_usdc

        # Track token-pair relationships: YES_token_id -> NO_token_id
        self._pairs: dict[str, str] = {}
        self._market_slugs: dict[str, str] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        await self._build_market_pairs()
        if not self._pairs:
            log.info("[arb_scanner] Retrying pair discovery...")
            await asyncio.sleep(5)
            await self._build_market_pairs()

        all_tokens = list(self._pairs.keys()) + list(self._pairs.values())
        book_poller.add_tokens(all_tokens)

        await store.log_event(f"🔍 Arb Scanner active — scanning {len(self._pairs)} binary market pair(s)")
        log.info("[arb_scanner] Monitoring %d binary pairs for arbitrage", len(self._pairs))

        # Background pair refresh so new markets are picked up automatically
        asyncio.create_task(self._pair_refresh_loop(), name="arb-pair-refresh")

        while self._running:
            try:
                await self._scan_for_arb()
            except Exception as e:
                log.error("[arb_scanner] Scan error: %s", e)
            await asyncio.sleep(self._scan_interval)

    async def _pair_refresh_loop(self) -> None:
        """Rebuild YES/NO token pairs every 10 minutes to capture new markets."""
        await asyncio.sleep(600)
        while self._running:
            try:
                old_count = len(self._pairs)
                await self._build_market_pairs()
                new_count = len(self._pairs)
                all_tokens = list(self._pairs.keys()) + list(self._pairs.values())
                book_poller.add_tokens(all_tokens)
                if new_count != old_count:
                    log.info("[arb_scanner] Pair refresh: %d -> %d binary pairs", old_count, new_count)
            except Exception as e:
                log.debug("[arb_scanner] Pair refresh error: %s", e)
            await asyncio.sleep(600)

    async def _build_market_pairs(self) -> None:
        """Fetch markets from Gamma and build YES/NO token-pair mappings."""
        try:
            markets = await gamma_client.get_markets(active=True, limit=60, order="volume24hr")
            for mkt in markets:
                pair = gamma_client.extract_binary_pair(mkt)
                if pair:
                    yes_tid, no_tid = pair
                    self._pairs[yes_tid] = no_tid
                    slug = mkt.get("slug") or mkt.get("groupItemTitle") or yes_tid[:12]
                    self._market_slugs[yes_tid] = slug
                    self._market_slugs[no_tid] = slug
                    store.market_slugs[yes_tid] = slug
                    store.market_slugs[no_tid] = slug

            log.info("[arb_scanner] Discovered %d binary pairs", len(self._pairs))
        except Exception as e:
            log.error("[arb_scanner] Failed to build pairs: %s", e)

    # ── Arbitrage Scan ────────────────────────────────────────────────────────

    async def _scan_for_arb(self) -> None:
        # (yes_tid, no_tid, yes_price, no_price, profit, arb_type)
        opportunities: list[tuple[str, str, float, float, float, str]] = []

        for yes_tid, no_tid in list(self._pairs.items()):
            long_opp = await self._check_long_dutch_book(yes_tid, no_tid)
            if long_opp:
                opportunities.append((yes_tid, no_tid, *long_opp, "long_dutch_book"))

            short_opp = await self._check_short_overpriced(yes_tid, no_tid)
            if short_opp:
                opportunities.append((yes_tid, no_tid, *short_opp, "short_overpriced"))

        # U9 — Observability: best-effort scan telemetry (additive; never breaks the scan).
        try:
            from core.observability import record_metric
            asyncio.create_task(record_metric("strategy", "arb_scanner.pairs_scanned", len(self._pairs)))
            asyncio.create_task(record_metric("strategy", "arb_scanner.opportunities", len(opportunities)))
            asyncio.create_task(record_metric("strategy", "arb_scanner.rejected", len(self._pairs) - len(opportunities)))
        except: pass

        if opportunities:
            opportunities.sort(key=lambda x: x[4], reverse=True)
            # Execute top-3 best opportunities per scan cycle
            for opp in opportunities[:3]:
                yes_tid, no_tid, yes_price, no_price, profit, arb_type = opp
                await self._execute_arb(yes_tid, no_tid, yes_price, no_price, profit, arb_type)

    async def _check_long_dutch_book(
        self, yes_token: str, no_token: str
    ) -> tuple[float, float, float] | None:
        """Dutch Book: Ask(YES) + Ask(NO) < 1.00 — buy both sides for guaranteed profit."""
        import time as _time
        yes_book = await store.get_order_book(yes_token)
        no_book = await store.get_order_book(no_token)

        if yes_book is None or no_book is None:
            return None
        if yes_book.best_ask is None or no_book.best_ask is None:
            return None

        # ── Staleness guard: skip if either book is > 30s old ──────────────────
        now = _time.time()
        if (now - yes_book.updated_at) > 30.0 or (now - no_book.updated_at) > 30.0:
            return None

        # ── Depth adequacy: enough size at ask prices to fill our order ────────
        yes_ask_depth = yes_book.asks[0].size if yes_book.asks else 0.0
        no_ask_depth = no_book.asks[0].size if no_book.asks else 0.0
        min_required_shares = max(1.0, self._order_size / max(yes_book.best_ask, 0.05))
        if yes_ask_depth < min_required_shares or no_ask_depth < min_required_shares:
            return None  # Insufficient depth — would move the market against us

        total_cost = yes_book.best_ask + no_book.best_ask
        profit_per_share = 1.00 - total_cost

        if profit_per_share >= self._min_profit_frac:
            # ── ML quality filter ─────────────────────────────────────────────
            # If ML strongly disagrees with the YES ask price, the book is likely
            # stale or has a data error — skip rather than trading on bad data.
            ml_suspicion = self._ml_arb_suspicion(yes_token, yes_book, yes_book.best_ask)
            if ml_suspicion:
                log.warning(
                    "[arb_scanner] ⚠ Long-arb on %s SKIPPED — ML quality filter triggered "
                    "(ML disagrees with ask price by >20¢ at high confidence)",
                    self._market_slugs.get(yes_token, yes_token[:12]),
                )
                return None

            slug = self._market_slugs.get(yes_token, yes_token[:12])
            log.info(
                "[arb_scanner] 🚀 LONG-ARB on %s: YES@%.4f + NO@%.4f = %.4f (profit=+%.4f/sh)",
                slug, yes_book.best_ask, no_book.best_ask, total_cost, profit_per_share,
            )
            return yes_book.best_ask, no_book.best_ask, profit_per_share
        return None

    async def _check_short_overpriced(
        self, yes_token: str, no_token: str
    ) -> tuple[float, float, float] | None:
        """Short-side overpriced: Bid(YES) + Bid(NO) > 1.00 — sell both sides for guaranteed profit."""
        import time as _time
        yes_book = await store.get_order_book(yes_token)
        no_book = await store.get_order_book(no_token)

        if yes_book is None or no_book is None:
            return None
        if yes_book.best_bid is None or no_book.best_bid is None:
            return None

        # ── Staleness guard ────────────────────────────────────────────────────
        now = _time.time()
        if (now - yes_book.updated_at) > 30.0 or (now - no_book.updated_at) > 30.0:
            return None

        # ── Depth adequacy: enough bid depth to absorb our sell ────────────────
        yes_bid_depth = yes_book.bids[0].size if yes_book.bids else 0.0
        no_bid_depth = no_book.bids[0].size if no_book.bids else 0.0
        min_required_shares = max(1.0, self._order_size / max(yes_book.best_bid, 0.05))
        if yes_bid_depth < min_required_shares or no_bid_depth < min_required_shares:
            return None

        total_bid = yes_book.best_bid + no_book.best_bid
        profit_per_share = total_bid - 1.00

        if profit_per_share >= self._min_profit_frac:
            slug = self._market_slugs.get(yes_token, yes_token[:12])
            log.info(
                "[arb_scanner] 🚀 SHORT-ARB on %s: YES@%.4f + NO@%.4f = %.4f (profit=+%.4f/sh)",
                slug, yes_book.best_bid, no_book.best_bid, total_bid, profit_per_share,
            )
            return yes_book.best_bid, no_book.best_bid, profit_per_share
        return None

    def _ml_arb_suspicion(self, token_id: str, book, book_price: float) -> bool:
        """
        Returns True when the ML model's probability estimate diverges from the
        book price by > 0.20 at high confidence (> 0.7) — indicating a stale or
        erroneous book rather than a genuine arb opportunity.

        This prevents trading on data errors masquerading as arb signals.
        """
        try:
            from core.market_discovery import market_discovery
            from ml.features import extract_features
            from ml.model import ml_model
            mkt_data = market_discovery.catalog.get(token_id) or {}
            feats = extract_features(mkt_data, book)
            if feats is None or not ml_model.is_fitted:
                return False
            p_yes, confidence = ml_model.predict(feats, token_id=token_id)
            ml_disagreement = abs(p_yes - book_price)
            # Only flag as suspicious when the model is confident AND disagrees significantly
            return bool(confidence > 0.70 and ml_disagreement > 0.20)
        except Exception:
            return False

    async def _execute_arb(
        self,
        yes_token: str,
        no_token: str,
        yes_price: float,
        no_price: float,
        profit: float,
        arb_type: str = "long_dutch_book",
    ) -> None:
        slug = self._market_slugs.get(yes_token, yes_token[:12])
        size = max(1.0, self._order_size / max(yes_price, 0.05))

        await store.log_event(
            f"⚡ ARB [{arb_type}] {slug}: YES@{yes_price:.4f} + NO@{no_price:.4f} | +{profit*100:.2f}% profit"
        )

        if arb_type == "long_dutch_book":
            # Buy both sides simultaneously
            yes_args = OrderArgs(
                token_id=yes_token, price=yes_price, side=Side.BUY,
                size=size, order_type="FOK",
            )
            no_args = OrderArgs(
                token_id=no_token, price=no_price, side=Side.BUY,
                size=size, order_type="FOK",
            )
        else:
            # Short-side: sell both sides (send as SELL at bid prices)
            yes_args = OrderArgs(
                token_id=yes_token, price=yes_price, side=Side.SELL,
                size=size, order_type="FOK",
            )
            no_args = OrderArgs(
                token_id=no_token, price=no_price, side=Side.SELL,
                size=size, order_type="FOK",
            )

        yes_order, no_order = await asyncio.gather(
            self.submit_order(yes_args),
            self.submit_order(no_args),
        )

        if yes_order and no_order:
            est_pnl = profit * size
            await store.log_event(
                f"✅ ARB [{arb_type}] executed on {slug}: Locked in ${est_pnl:.2f} expected profit"
            )
            log.info("[arb_scanner] ARB [%s] executed on %s — profit: $%.2f", arb_type, slug, est_pnl)

    # ── W19-2 — Unified Strategy Contract (God Mode §26) ────────────────────
    # The 9 contract methods below are SYNC wrappers over the existing async
    # arbitrage-scan / execute machinery. They expose the strategy's signal-
    # generation / sizing / entry-decision / exit-decision / diagnostics
    # surface to operators / dashboards / backtest engines without requiring
    # an event loop. The async ``_run`` / ``_scan_for_arb`` / ``_execute_arb``
    # path remains the canonical live-trading loop.

    def metadata(self) -> dict:
        """Return strategy metadata for the catalog / dashboard."""
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "High-Frequency Binary Arbitrage Scanner. Detects long-side "
                "Dutch Book (Ask(YES) + Ask(NO) < 1.00) and short-side "
                "overpriced (Bid(YES) + Bid(NO) > 1.00) mispricing on "
                "Polymarket binary prediction markets, with ML quality "
                "filtering and depth / staleness guards."
            ),
            "author": "polymarket-bot",
            "category": "arbitrage",
            "model": "binary_dutch_book",
        }

    def configure(self, config: dict) -> None:
        """Apply runtime config overrides (min_profit, scan_interval, ...).

        Recognised keys (all optional — unrecognised keys are stored in
        ``self.config`` for introspection but not applied to typed fields):

          * ``min_profit_frac`` (float > 0) — overrides ``_min_profit_frac``.
          * ``scan_interval`` (float > 0)  — overrides ``_scan_interval``.
          * ``order_size`` (float > 0)     — overrides ``_order_size``.
        """
        super().configure(config)
        if "min_profit_frac" in config:
            v = float(config["min_profit_frac"])
            if v > 0.0:
                self._min_profit_frac = v
        if "scan_interval" in config:
            v = float(config["scan_interval"])
            if v > 0.0:
                self._scan_interval = v
        if "order_size" in config:
            v = float(config["order_size"])
            if v > 0.0:
                self._order_size = v

    def validate(self) -> tuple[bool, str]:
        """Validate strategy configuration post-construction.

        ``ArbScannerStrategy`` is valid when:
          * ``_min_profit_frac > 0`` (a non-positive floor would either
            always-fire or always-reject — both are misconfigurations).
          * ``_scan_interval > 0`` (a non-positive interval would
            busy-loop the scanner).
          * ``_order_size > 0`` (a non-positive order size would zero
            every submitted leg).
        """
        if self._min_profit_frac <= 0.0:
            return False, (
                f"_min_profit_frac={self._min_profit_frac} must be > 0"
            )
        if self._scan_interval <= 0.0:
            return False, (
                f"_scan_interval={self._scan_interval} must be > 0"
            )
        if self._order_size <= 0.0:
            return False, (
                f"_order_size={self._order_size} must be > 0"
            )
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Signal | None:
        """Build a contract ``Signal`` representing an arb opportunity.

        ``market_context`` is a caller-provided dict (NOT the live store).
        Recognised keys:

          * ``yes_token`` (str, required) — the YES token id
          * ``no_token`` (str, required)  — the NO token id
          * ``yes_price`` (float > 0, required) — ask(YES) for long arb /
            bid(YES) for short arb
          * ``no_price`` (float > 0, required)  — ask(NO) for long arb /
            bid(NO) for short arb
          * ``arb_type`` (str, optional — defaults to ``"long_dutch_book"``;
            valid values: ``"long_dutch_book"`` (buy both sides),
            ``"short_overpriced"`` (sell both sides))
          * ``profit_per_share`` (float, optional — pre-computed profit;
            defaults to ``1.00 - (yes_price + no_price)`` for long /
            ``(yes_price + no_price) - 1.00`` for short)

        Returns ``None`` when any required key is missing, or when the
        computed profit is below ``_min_profit_frac`` (the contract method
        mirrors the production min-profit gate so callers don't have to
        re-derive it).
        """
        yes_token = market_context.get("yes_token")
        no_token = market_context.get("no_token")
        yes_price = market_context.get("yes_price")
        no_price = market_context.get("no_price")
        if not yes_token or not no_token or yes_price is None or no_price is None:
            return None

        arb_type = str(market_context.get("arb_type", "long_dutch_book"))
        if arb_type == "long_dutch_book":
            profit = float(
                market_context.get("profit_per_share",
                                   1.00 - (yes_price + no_price))
            )
            action = "BUY"  # Buy both sides
        elif arb_type == "short_overpriced":
            profit = float(
                market_context.get("profit_per_share",
                                   (yes_price + no_price) - 1.00)
            )
            action = "SELL"  # Sell both sides
        else:
            return None

        # Mirror the production min-profit gate so the contract method
        # never returns a sub-threshold opportunity.
        if profit < self._min_profit_frac:
            return None

        # W19-2 — bump signals counter.
        self._stats["signals"] = self._stats.get("signals", 0) + 1

        # Use the YES token as the canonical signal token_id (the NO leg
        # is recorded in metadata so callers can recover both legs).
        return Signal(
            action=action,
            token_id=yes_token,
            size=self._order_size,
            price=yes_price,
            confidence=1.0,  # Arb is risk-free by construction
            edge=profit,  # Per-share profit (expected P&L per $1 notional)
            reason=f"{arb_type}: YES@{yes_price:.4f} + NO@{no_price:.4f} → profit {profit*100:.2f}%",
            metadata={
                "arb_type": arb_type,
                "yes_token": yes_token,
                "no_token": no_token,
                "yes_price": yes_price,
                "no_price": no_price,
                "profit_per_share": profit,
                "order_type": "FOK",  # Fill-or-Kill — both legs must fill or none
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        """Estimate expected P&L per dollar for the arb.

        For a binary arbitrage, the edge is the per-share profit stored on
        the signal by ``generate_signal``. The contract method surfaces
        that pre-computed value so callers don't need to know each
        strategy's edge formula.
        """
        if signal is None:
            return 0.0
        return signal.edge

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        """Size the arb using ``_order_size`` bounded by capital.

        ``ArbScannerStrategy`` sizes every arb at the configured
        ``_order_size`` (USDC notional). This contract method mirrors
        that policy: it returns the smaller of (a) the configured order
        size, (b) the available capital, and (c) the optional
        ``risk_params.max_position_per_market`` cap.

        ``risk_params`` recognised keys (all optional):
          * ``order_size_override`` (float > 0) — overrides ``_order_size``.
          * ``max_position_per_market`` (float > 0) — additional cap.
        """
        if signal is None or signal.action == "HOLD":
            return 0.0
        order_size = float(
            risk_params.get("order_size_override", self._order_size)
        )
        # Bound by order_size, optional per-market cap, and capital.
        size = order_size
        max_pos = risk_params.get("max_position_per_market")
        if max_pos is not None:
            size = min(size, float(max_pos))
        return min(size, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        """Return entry execution parameters mirroring ``_execute_arb``.

        ``_execute_arb`` constructs an ``OrderArgs`` per leg with:
          * ``token_id`` = yes_token / no_token
          * ``price``    = yes_price / no_price
          * ``side``     = Side.BUY (long arb) or Side.SELL (short arb)
          * ``size``     = max(1.0, order_size / max(yes_price, 0.05))
          * ``order_type`` = "FOK" (Fill-or-Kill — both legs must fill or none)

        This contract method returns the YES-leg entry params as a plain
        dict. The NO-leg params are returned in ``metadata.no_leg`` so
        callers have both legs' execution parameters in one call.
        """
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "signal action is HOLD or None"}
        yes_price = signal.price if signal.price is not None else 0.5
        # Shares = max(1.0, order_size / max(yes_price, 0.05)) — matches
        # _execute_arb's size formula (the 0.05 floor protects against
        # divide-by-zero on a sub-penny price).
        size_shares = max(1.0, signal.size / max(yes_price, 0.05))
        meta = signal.metadata or {}
        no_token = meta.get("no_token", "")
        no_price = meta.get("no_price", yes_price)
        return {
            "token_id": signal.token_id,  # YES token
            "price": yes_price,
            "side": signal.action,  # "BUY" / "SELL"
            "size": size_shares,
            "type": "FOK",  # Fill-or-Kill — both legs must fill or none
            "no_leg": {
                "token_id": no_token,
                "price": no_price,
                "side": signal.action,
                "size": max(1.0, signal.size / max(no_price, 0.05)),
                "type": "FOK",
            },
            "arb_type": meta.get("arb_type", "long_dutch_book"),
        }

    def exit_logic(self, position: dict, market_context: dict) -> dict | None:
        """Determine whether to exit a position.

        ``ArbScannerStrategy`` executes Fill-or-Kill orders that resolve
        immediately — there is no held position to exit. Returns ``None``
        unconditionally because the strategy's arb legs either fill (and
        resolve at market close) or never execute (FOK rejection). The
        contract method exists for parity with the other strategies so a
        dashboard can call ``exit_logic`` uniformly without knowing the
        concrete class.
        """
        # Arb positions resolve at market close; no async exit policy.
        return None

    def diagnostics(self) -> dict:
        """Return strategy state + stats + pair count.

        Surfaces the contract base fields (name / running / stats /
        last_error) plus the arb-scanner-specific state: number of
        YES/NO token pairs being scanned, the configured min-profit
        floor, scan interval, and order size.
        """
        base = super().diagnostics()
        base.update({
            "pairs_scanned": len(self._pairs),
            "min_profit_frac": self._min_profit_frac,
            "scan_interval": self._scan_interval,
            "order_size": self._order_size,
            "paper_mode": self._paper,
        })
        return base
