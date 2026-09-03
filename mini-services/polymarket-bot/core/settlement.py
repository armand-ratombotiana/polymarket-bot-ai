"""
core/settlement.py — Market Resolution Settlement & ML Feedback Engine.

Periodically queries Gamma API for resolved markets:
1. Calculates real/paper PnL for any open positions in resolved markets
   - Winning outcome: shares * $1.00 - invested cost
   - Losing outcome: -$invested cost
2. Settles and closes the positions in DataStore
3. Automatically feeds the ground-truth outcome (resolved_yes) into ml_model
   for continuous online learning 24/7!
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from core.data_store import BANKROLL_BASELINE, Side, Trade, store
from core.gamma_client import gamma_client

log = logging.getLogger(__name__)

SETTLEMENT_CHECK_INTERVAL = 60.0  # check every 60s


class SettlementEngine:
    """
    Settles resolved prediction markets, updates portfolio balance/PnL,
    and feeds ground truth labels to the ML online learner.
    """

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._settled_tokens: set[str] = set()

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="settlement-engine")
        log.info("[settlement] Settlement engine started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        await asyncio.sleep(15.0)  # Initial grace period
        while self._running:
            try:
                await self._check_resolved_markets()
            except Exception as e:
                log.debug("[settlement] Settlement check error: %s", e)
            await asyncio.sleep(SETTLEMENT_CHECK_INTERVAL)

    async def _check_resolved_markets(self) -> None:
        resolved_mkts = await gamma_client.get_resolved_markets(limit=20)
        for mkt in resolved_mkts:
            await self._process_resolved_market(mkt)

    async def _process_resolved_market(self, mkt: dict) -> None:
        token_ids = gamma_client.extract_token_ids(mkt)
        if not token_ids:
            return

        yes_token = token_ids[0]
        if yes_token in self._settled_tokens:
            return

        # Determine winner: outcomePrices or resolvedBy outcome
        outcome_prices = mkt.get("outcomePrices")
        resolved_yes = False
        if outcome_prices:
            if isinstance(outcome_prices, str):
                try:
                    prices = json.loads(outcome_prices)
                except Exception:
                    prices = []
            else:
                prices = outcome_prices

            if prices and len(prices) >= 2:
                p0 = float(prices[0])
                resolved_yes = (p0 >= 0.9)

        slug = mkt.get("slug") or store.market_slugs.get(yes_token, yes_token[:12])
        no_token = token_ids[1] if len(token_ids) > 1 else None

        # X8 — Capture log_event message text inside the lock but emit the actual
        # log_event call AFTER the lock is released. DataStore.log_event
        # re-acquires store._lock internally, and asyncio.Lock is NOT reentrant,
        # so calling `await store.log_event(...)` while already holding
        # store._lock deadlocks the settlement loop forever. (Nested-lock
        # deadlock found by U2 subagent.)
        _x8_yes_log_msg: str | None = None
        _x8_no_log_msg: str | None = None

        # Settle any active positions in DataStore (both YES and NO tokens)
        async with store._lock:
            # 1. Settle YES token
            pos_yes = store.positions.get(yes_token)
            if pos_yes and (pos_yes.yes_shares > 0 or pos_yes.total_invested > 0):
                shares = pos_yes.yes_shares
                payout = shares * 1.0 if resolved_yes else 0.0
                pnl = payout - pos_yes.total_invested

                trade_yes = Trade(
                    trade_id=f"settle-yes-{yes_token[:8]}",
                    token_id=yes_token,
                    side=Side.SELL,
                    price=1.0 if resolved_yes else 0.0,
                    size=shares,
                    pnl=pnl,
                    strategy="settlement",
                    paper=True,
                )
                store.trades.append(trade_yes)
                store.daily_pnl += pnl
                store.paper_balance += payout
                current_eq = BANKROLL_BASELINE + store.daily_pnl
                store.peak_equity = max(store.peak_equity, current_eq)
                store.equity_history.append({
                    "timestamp": time.time(),
                    "equity": round(current_eq, 2),
                })
                del store.positions[yes_token]
                log.info(
                    "[settlement] Resolved YES token %s (%s): PnL=$%.2f (Payout=$%.2f, Invested=$%.2f)",
                    slug, "WINNER" if resolved_yes else "ZERO", pnl, payout, pos_yes.total_invested,
                )
                _x8_yes_log_msg = (
                    f"🏆 Settlement: {slug} YES -> {'WINNER ($1.00)' if resolved_yes else '$0.00'} | PnL: ${pnl:+.2f}"
                )

                # V11 — Mirror the settled YES position into the closed-positions
                # journal so the round-trip is queryable for attribution /
                # post-hoc analytics. Wrapped in try/except so a journal hiccup
                # can never break the trading pipeline.
                try:
                    from core.closed_positions import closed_positions
                    from ml.model import ml_model
                    await closed_positions.record_closed_position(
                        token_id=yes_token,
                        strategy=getattr(pos_yes, 'strategy', 'settlement'),
                        entry_price=pos_yes.avg_entry_price,
                        exit_price=1.0 if resolved_yes else 0.0,
                        shares=shares,
                        pnl=pnl,
                        holding_seconds=time.time() - getattr(pos_yes, 'opened_at', time.time()),
                        model_version=getattr(ml_model, '_last_trained', 'unknown'),
                    )
                except Exception:
                    pass

            # 2. Settle NO token (if present)
            if no_token:
                pos_no = store.positions.get(no_token)
                if pos_no and (pos_no.yes_shares > 0 or pos_no.total_invested > 0):
                    shares_no = pos_no.yes_shares
                    resolved_no = not resolved_yes
                    payout_no = shares_no * 1.0 if resolved_no else 0.0
                    pnl_no = payout_no - pos_no.total_invested

                    trade_no = Trade(
                        trade_id=f"settle-no-{no_token[:8]}",
                        token_id=no_token,
                        side=Side.SELL,
                        price=1.0 if resolved_no else 0.0,
                        size=shares_no,
                        pnl=pnl_no,
                        strategy="settlement",
                        paper=True,
                    )
                    store.trades.append(trade_no)
                    store.daily_pnl += pnl_no
                    store.paper_balance += payout_no
                    current_eq = BANKROLL_BASELINE + store.daily_pnl
                    store.peak_equity = max(store.peak_equity, current_eq)
                    store.equity_history.append({
                        "timestamp": time.time(),
                        "equity": round(current_eq, 2),
                    })
                    del store.positions[no_token]
                    log.info(
                        "[settlement] Resolved NO token %s (%s): PnL=$%.2f (Payout=$%.2f, Invested=$%.2f)",
                        slug, "WINNER" if resolved_no else "ZERO", pnl_no, payout_no, pos_no.total_invested,
                    )
                    _x8_no_log_msg = (
                        f"🏆 Settlement: {slug} NO -> {'WINNER ($1.00)' if resolved_no else '$0.00'} | PnL: ${pnl_no:+.2f}"
                    )

                    # V11 — Mirror the settled NO position into the closed-positions
                    # journal (mirrors the YES branch above). Wrapped in try/except
                    # so a journal hiccup can never break the trading pipeline.
                    try:
                        from core.closed_positions import closed_positions
                        from ml.model import ml_model
                        await closed_positions.record_closed_position(
                            token_id=no_token,
                            strategy=getattr(pos_no, 'strategy', 'settlement'),
                            entry_price=pos_no.avg_entry_price,
                            exit_price=1.0 if resolved_no else 0.0,
                            shares=shares_no,
                            pnl=pnl_no,
                            holding_seconds=time.time() - getattr(pos_no, 'opened_at', time.time()),
                            model_version=getattr(ml_model, '_last_trained', 'unknown'),
                        )
                    except Exception:
                        pass

        # X8 — Lock has been released; now safe to call store.log_event(),
        # which re-acquires store._lock internally. Emit any captured messages.
        if _x8_yes_log_msg is not None:
            try:
                await store.log_event(_x8_yes_log_msg)
            except Exception as e:
                log.warning("[settlement] YES log_event failed: %s", e)
        if _x8_no_log_msg is not None:
            try:
                await store.log_event(_x8_no_log_msg)
            except Exception as e:
                log.warning("[settlement] NO log_event failed: %s", e)

        self._settled_tokens.add(yes_token)
        if no_token:
            self._settled_tokens.add(no_token)

        # ── Ground-truth backfill & ML online learning ──────────────────────────
        # Two-step feedback loop:
        #   1. Persist resolved label to SQLite / TimescaleDB for batch retraining
        #   2. Immediately update the live SGD online learner with the ground-truth
        #      so the model adapts in real time without waiting for a full retrain cycle.
        #
        # Previously step 2 was MISSING — the SGD learner accumulated predictions
        # but received zero outcome feedback, making online updates a no-op.
        try:
            from core.timescale_db import timescale_db
            updated = timescale_db.mark_resolved_outcomes(yes_token, resolved_yes=resolved_yes)
            if updated:
                log.info("[settlement] Backfilled %d feature-store labels for %s (YES=%s)",
                         updated, yes_token, resolved_yes)

            # Fetch the cached feature vector for this token and trigger online update
            feat_vec = timescale_db.fetch_recent_feature_vector(yes_token)
            if feat_vec is not None:
                from ml.model import ml_model
                ml_model.update(feat_vec, outcome_yes=resolved_yes)
                log.info("[settlement] ✅ ML online update: %s resolved=%s "
                         "(SGD updates: %d total)",
                         slug, "YES" if resolved_yes else "NO", ml_model._n_updates)
            else:
                log.debug("[settlement] No cached feature vector for %s — online update skipped", yes_token)

            # Also mark NO token's feature vectors if present
            if no_token:
                timescale_db.mark_resolved_outcomes(no_token, resolved_yes=not resolved_yes)
        except Exception as e:
            log.error("[settlement] Outcome backfill / ML update failed for %s: %s", yes_token, e)


# Module-level singleton
settlement_engine = SettlementEngine()
