"""
strategies/registry.py — Central 50+ Quantitative Strategy Factory & Orchestrator.

Manages strategy taxonomy, instantiation, live toggling, execution loops,
and performance attribution across 6 quantitative trading archetypes (50 total strategies).

W19-6 — Honest status reporting. Each strategy now carries a ``status``
field that distinguishes IMPLEMENTED (real trading logic) from PLANNED
(no-op stub). The API exposes ``?implemented_only=true`` so the UI can
filter the catalog to show only strategies that actually execute.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from strategies.base import BaseStrategy

log = logging.getLogger(__name__)


# ── Strategy lifecycle status ─────────────────────────────────────────────────
# IMPLEMENTED   — has a real ``_run`` trading loop (submits orders, scans
#                 markets, generates signals). Backed by a concrete class.
# PLANNED       — catalog entry only. ``_execute_cycle`` is a no-op ``pass``
#                 and ``QuantStrategyInstance`` is the placeholder wrapper.
# EXPERIMENTAL  — has logic but is gated behind a feature flag or under
#                 active evaluation (not yet trusted for live capital).
STATUS_IMPLEMENTED = "IMPLEMENTED"
STATUS_PLANNED = "PLANNED"
STATUS_EXPERIMENTAL = "EXPERIMENTAL"


@dataclass
class StrategyMeta:
    strategy_id: str
    name: str
    category: str
    description: str
    risk_level: str
    default_enabled: bool = False
    # W19-6 — honest status reporting. ``IMPLEMENTED`` strategies have a
    # real trading loop backed by a concrete strategy class; ``PLANNED``
    # entries are no-op stubs (``QuantStrategyInstance``); ``EXPERIMENTAL``
    # is reserved for feature-flagged work-in-progress.
    status: str = STATUS_PLANNED


# ── 50 Strategy Metadata Catalog ──────────────────────────────────────────────
# Status legend: IMPLEMENTED = real trading loop; PLANNED = no-op stub.
# Sixteen IMPLEMENTED strategies (3 original + 3 W19-6 + 5 W22-3 + 5 W44-1):
#   • mm_avellaneda_stoikov       → MarketMakerStrategy
#   • arb_binary_dutch_book       → ArbScannerStrategy
#   • ml_random_forest_quant      → SignalTraderStrategy
#   • stat_ornstein_uhlenbeck     → MeanReversionStrategy (W19-6)
#   • mom_macd_histogram          → MomentumStrategy      (W19-6)
#   • ml_isotonic_calibrated      → ValueStrategy         (W19-6)
#   • arb_cross_correlation       → StatisticalArbitrage  (W22-3)
#   • event_news_sentiment        → EventDriven           (W22-3)
#   • event_resolution_sniper      → Convergence           (W22-3)
#   • mm_asymmetric_spread         → SpreadCapture         (W22-3)
#   • mm_grid_liquidity            → LiquidityProvision    (W22-3)
#   • arb_temporal_expiry          → LateResolution        (W44-1)
#   • ml_fractional_kelly          → Ensemble              (W44-1)
#   • event_poll_discrepancy       → NewsTrader            (W44-1)
#   • event_social_volume          → SentimentAggregator   (W44-1)
#   • arb_cluster_dislocation      → CrossMarket           (W44-1)

STRATEGY_CATALOG: list[StrategyMeta] = [
    # ── Group A: Market Making & Liquidity Provision (8) ──
    StrategyMeta("mm_avellaneda_stoikov", "Avellaneda-Stoikov MM", "market_making", "Reservation price with inventory skewing & volatility bounds", "Medium", True, status=STATUS_IMPLEMENTED),
    StrategyMeta("mm_glft_optimal", "GLFT Optimal Quoter", "market_making", "Gueant-Tapia-Manziadi intensity-based optimal quote spread", "Medium", False),
    StrategyMeta("mm_asymmetric_spread", "Asymmetric Spread Skew", "market_making", "Skewed bid/ask width based on directional order flow momentum", "Medium", False, status=STATUS_IMPLEMENTED),
    StrategyMeta("mm_volatility_adaptive", "Volatility Adaptive MM", "market_making", "Dynamic spread widening/narrowing based on ATR & realized vol", "Low", False),
    StrategyMeta("mm_rebate_harvester", "Rebate Harvester", "market_making", "Maximizes maker fee rebates at top-of-book with queue priority", "Low", False),
    StrategyMeta("mm_ofi_microstructure", "Order Flow Imbalance MM", "market_making", "Real-time micro-depth OFI quotes against toxic adverse selection", "Medium", False),
    StrategyMeta("mm_grid_liquidity", "Grid Trading Liquidity", "market_making", "Multi-level layered limit orders with step-ladder profit taking", "Medium", False, status=STATUS_IMPLEMENTED),
    StrategyMeta("mm_poisson_arrival", "Poisson Arrival Quoter", "market_making", "Quoting calibrated to continuous trade arrival intensity lambda(p)", "Low", False),

    # ── Group B: Arbitrage & Relative Value (8) ──
    StrategyMeta("arb_binary_dutch_book", "Binary Dutch Book", "arbitrage", "Guaranteed payout arbitrage when Ask(YES) + Ask(NO) < 1.00 - fees", "Low", True, status=STATUS_IMPLEMENTED),
    StrategyMeta("arb_multi_negative_risk", "Negative Risk Multi-Arb", "arbitrage", "Combinatorial arbitrage across N-outcome mutually exclusive events", "Low", False),
    StrategyMeta("arb_gamma_clob_parity", "Gamma-CLOB Parity Arb", "arbitrage", "Exploits pricing dislocations between Gamma AMM and CLOB books", "Low", False),
    StrategyMeta("arb_synthetic_straddle", "Synthetic Straddle Arb", "arbitrage", "Exploits implied volatility mispricing on paired event outcomes", "Medium", False),
    StrategyMeta("arb_temporal_expiry", "Late Resolution (Decay Curve)", "arbitrage", "W44-1: trades late-resolution decay — BUY when the observed mid is below the modeled logistic-decay fair value (market under-pricing near-certain outcome), SELL when above (over-pricing); uses a logistic decay curve with a 6h half-life and 0.5 steepness", "Low", False, status=STATUS_IMPLEMENTED),
    StrategyMeta("arb_cross_correlation", "Cross-Category Arb", "arbitrage", "Pairs trading on economically correlated event groups (crypto/macro)", "Medium", False, status=STATUS_IMPLEMENTED),
    StrategyMeta("arb_cluster_dislocation", "Cross-Market Cluster Dislocation", "arbitrage", "W44-1: trades cluster dislocations — BUY the most-dislocated under-priced cluster member and SELL the over-priced one when the member's z-score against the cluster mean exceeds 1.5σ with cluster correlation ≥ 0.55 and ≥ 3 members", "Low", False, status=STATUS_IMPLEMENTED),
    StrategyMeta("arb_cyclic_triangle", "Cyclic Triangle Arb", "arbitrage", "Triangle arbitrage across multi-condition chained prediction markets", "Low", False),

    # ── Group C: Statistical Arbitrage & Mean Reversion (8) ──
    StrategyMeta("stat_bollinger_reversion", "Bollinger Bands Reversion", "statistical", "Buys/sells when price touches 2.5-sigma bands and mean-reverts", "Medium", False),
    StrategyMeta("stat_ornstein_uhlenbeck", "Mean Reversion (Bollinger Bands)", "statistical", "W19-6: trades mean-reversion — BUY when price breaches the lower Bollinger Band, SELL when price breaches the upper band, with a rolling-window MA + sigma estimator", "Medium", False, status=STATUS_IMPLEMENTED),
    StrategyMeta("stat_rsi_divergence", "RSI Divergence Mean Rev", "statistical", "Identifies overbought (RSI>80) and oversold (RSI<20) exhaustion", "Medium", False),
    StrategyMeta("stat_zscore_anomaly", "Z-Score Anomaly Trader", "statistical", "Outlier detection on price deviation from volume-weighted mean", "Medium", False),
    StrategyMeta("stat_pair_cointegration", "Pair Cointegration Trader", "statistical", "Augmented Dickey-Fuller cointegrated spread mean-reversion", "Low", False),
    StrategyMeta("stat_vwap_reversion", "VWAP Pullback Reversion", "statistical", "Trades mean-reversion toward Volume Weighted Average Price", "Low", False),
    StrategyMeta("stat_kalman_filter", "Kalman Filter Fair Value", "statistical", "State-space Kalman filter tracking true underlying fair value", "Low", False),
    StrategyMeta("stat_half_life_decay", "Half-Life Decay Reverter", "statistical", "Calibrates trade horizon to statistical mean-reversion half-life", "Medium", False),

    # ── Group D: Momentum, Breakout & Trend Following (8) ──
    StrategyMeta("mom_ema_crossover", "EMA Crossover Trend", "momentum", "Fast/Slow Exponential Moving Average trend capture (8/21 EMA)", "Medium", False),
    StrategyMeta("mom_macd_histogram", "Momentum (Rate of Change)", "momentum", "W19-6: trades momentum — BUY when ROC (Rate of Change) is strongly positive, SELL when momentum reverses; uses a 10-cycle ROC window with ±5% thresholds", "Medium", False, status=STATUS_IMPLEMENTED),
    StrategyMeta("mom_donchian_breakout", "Donchian Channel Breakout", "momentum", "20-period high/low breakout momentum with trailing stops", "High", False),
    StrategyMeta("mom_volatility_expansion", "Volatility Expansion Trend", "momentum", "Enters explosive trend regimes following ATR volatility squeeze", "High", False),
    StrategyMeta("mom_volume_surge", "Volume Surge Momentum", "momentum", "Follows sudden 3x volume spikes with directional price breakout", "High", False),
    StrategyMeta("mom_parabolic_sar", "Parabolic SAR Follower", "momentum", "Trend-following with dynamic trailing stop and reverse points", "Medium", False),
    StrategyMeta("mom_adx_trend_strength", "ADX Trend Strength", "momentum", "Filters and trades only strong directional trends (ADX > 25)", "Medium", False),
    StrategyMeta("mom_micro_price_accel", "Micro-Price Acceleration", "momentum", "Fast execution following micro-price acceleration (P_micro - P_mid)", "High", False),

    # ── Group E: Event-Driven, Sentiment & Intelligence (8) ──
    StrategyMeta("event_news_sentiment", "News Sentiment Breakout", "event_driven", "NLP sentiment scoring on breaking news feeds to trade probability shifts", "Medium", False, status=STATUS_IMPLEMENTED),
    StrategyMeta("event_social_volume", "Sentiment (Social Aggregator)", "event_driven", "W44-1: trades aggregated social sentiment — BUY when the current aggregated sentiment z-score against a rolling 50-cycle baseline exceeds +2σ (bullish shift), SELL when below -2σ (bearish shift); weights by source-diversity across ≥ 2 platforms and ≥ 100 mentions", "Medium", False, status=STATUS_IMPLEMENTED),
    StrategyMeta("event_poll_discrepancy", "News (Polling Gap)", "event_driven", "W44-1: trades polling-vs-price discrepancies — BUY when poll_probability - mid exceeds the polling margin of error + 2.5% buffer (market under-prices YES outcome), SELL in the symmetric case; skips polls with < 500 respondents or > 72h staleness", "Medium", False, status=STATUS_IMPLEMENTED),
    StrategyMeta("event_oracle_dispute", "Oracle Dispute Sniper", "event_driven", "Positions ahead of UMA resolution disputes and bond challenges", "High", False),
    StrategyMeta("event_election_momentum", "Election Momentum Tracker", "event_driven", "Tracks polling momentum shifts in political & election markets", "Medium", False),
    StrategyMeta("event_macro_straddle", "Macro Announcement Straddle", "event_driven", "Pre-positions ahead of CPI/FOMC/jobs reports using straddle execution", "Medium", False),
    StrategyMeta("event_whale_follower", "Whale Block Order Follower", "event_driven", "Detects institutional block orders (> $5,000) and rides market impact", "Medium", False),
    StrategyMeta("event_resolution_sniper", "Resolution Expiry Sniper", "event_driven", "High-conviction sniper executing in final 24h of near-certain events", "Low", False, status=STATUS_IMPLEMENTED),

    # ── Group F: Machine Learning & Reinforcement Learning (10) ──
    StrategyMeta("ml_lightgbm_boost", "LightGBM Gradient Boost", "machine_learning", "Ultra-fast gradient boosted decision tree classifier with calibrated probs", "Medium", False),
    StrategyMeta("ml_xgboost_directional", "XGBoost Directional", "machine_learning", "Regularized gradient boosting model on order flow & volume dynamics", "Medium", False),
    StrategyMeta("ml_random_forest_quant", "Random Forest Quant Model", "machine_learning", "Multi-factor bagging ensemble of 100 decision trees", "Low", True, status=STATUS_IMPLEMENTED),
    StrategyMeta("ml_online_sgd_learner", "Online SGD Momentum", "machine_learning", "Real-time passive-aggressive incremental learner updating from every fill", "Medium", False),
    StrategyMeta("ml_fractional_kelly", "Ensemble (Fractional Kelly)", "machine_learning", "W44-1: ensemble meta-strategy — aggregates BUY/SELL signals from N upstream strategies via weighted-vote (min_confidence ≥ 0.40, vote margin ≥ 10%), computes the aggregated edge as the weighted-mean of the concurring signals' edges, and sizes the consensus position via quarter-Kelly (kelly_fraction = 0.25)", "Low", False, status=STATUS_IMPLEMENTED),
    StrategyMeta("ml_isotonic_calibrated", "Value (ML Fair Value)", "machine_learning", "W19-6: trades mispriced markets — BUY when ML model p_yes >> market mid, SELL when model p_yes << market mid; uses the ensemble model for fair-value estimation with a 5% minimum edge gate", "Low", False, status=STATUS_IMPLEMENTED),
    StrategyMeta("ml_gmm_regime_switch", "GMM Regime Switching", "machine_learning", "Gaussian Mixture Model identifying high-vol vs low-vol market regimes", "Medium", False),
    StrategyMeta("ml_svm_hyperplane", "SVM Hyperplane Classifier", "machine_learning", "Non-linear RBF kernel hyperplane separator for market state classification", "Medium", False),
    StrategyMeta("ml_bayesian_belief", "Bayesian Belief Updater", "machine_learning", "Bayesian posterior probability updates based on new evidence arrival", "Low", False),
    StrategyMeta("ml_qlearning_execution", "Q-Learning Execution Agent", "machine_learning", "Reinforcement learning agent optimizing limit order placement & timing", "Medium", False),
]


# ── Strategy ID → concrete class mapping ──────────────────────────────────────
# W19-6 — every IMPLEMENTED strategy maps to a concrete ``BaseStrategy``
# subclass with a real ``_run`` trading loop. PLANNED entries fall
# through to the generic ``QuantStrategyInstance`` no-op wrapper.
_IMPLEMENTED_STRATEGY_CLASSES: dict[str, str] = {
    # The three original concrete strategies (Wave 1–8 era).
    "mm_avellaneda_stoikov": "strategies.market_maker.MarketMakerStrategy",
    "arb_binary_dutch_book": "strategies.arb_scanner.ArbScannerStrategy",
    "ml_random_forest_quant": "strategies.signal_trader.SignalTraderStrategy",
    # The three W19-6 additions.
    "stat_ornstein_uhlenbeck": "strategies.mean_reversion.MeanReversionStrategy",
    "mom_macd_histogram": "strategies.momentum.MomentumStrategy",
    "ml_isotonic_calibrated": "strategies.value.ValueStrategy",
    # The five W22-3 additions — promoted from PLANNED to IMPLEMENTED.
    "arb_cross_correlation": "strategies.stat_arb.StatisticalArbitrage",
    "event_news_sentiment": "strategies.event_driven.EventDriven",
    "event_resolution_sniper": "strategies.convergence.Convergence",
    "mm_asymmetric_spread": "strategies.spread_capture.SpreadCapture",
    "mm_grid_liquidity": "strategies.liquidity.LiquidityProvision",
    # The five W44-1 additions — promoted from PLANNED to IMPLEMENTED.
    "arb_temporal_expiry": "strategies.late_resolution.LateResolution",
    "ml_fractional_kelly": "strategies.ensemble.Ensemble",
    "event_poll_discrepancy": "strategies.news.NewsTrader",
    "event_social_volume": "strategies.sentiment.SentimentAggregator",
    "arb_cluster_dislocation": "strategies.cross_market.CrossMarket",
}

# Legacy aliases — the registry accepts these alternative ids for backward
# compatibility with the existing API surface (e.g. ``POST /api/strategies
# /toggle`` was previously called with the bare class names).
_LEGACY_ALIASES: dict[str, str] = {
    "market_maker": "mm_avellaneda_stoikov",
    "arb_scanner": "arb_binary_dutch_book",
    "signal_trader": "ml_random_forest_quant",
}


def _is_implemented(strategy_id: str) -> bool:
    """Return True iff the strategy_id maps to a concrete class."""
    return strategy_id in _IMPLEMENTED_STRATEGY_CLASSES


class QuantStrategyInstance(BaseStrategy):
    """
    Modular quantitative execution wrapper executing any strategy in the catalog
    with parameterized mathematical signals.

    W19-6 — explicit PLANNED marker. ``_execute_cycle`` remains a no-op
    ``pass`` for every strategy that has no concrete class; the registry
    no longer pretends these are real. ``status`` on the catalog row
    surfaces this distinction to API consumers.
    """

    def __init__(self, meta: StrategyMeta) -> None:
        super().__init__()
        self.meta = meta
        self.name = meta.strategy_id
        self._interval = 5.0
        self._active_orders: dict[str, str] = {}

    async def _run(self) -> None:
        log.info("[strategy_hub] Started [%s] (%s) — PLANNED, no-op loop", self.meta.name, self.meta.category)
        while self._running:
            try:
                await self._execute_cycle()
            except Exception as e:
                log.debug("[%s] Cycle error: %s", self.name, e)
            await asyncio.sleep(self._interval)

    async def _execute_cycle(self) -> None:
        # Specialized logic handled per category archetype
        pass


class StrategyRegistry:
    """
    Central strategy factory and lifecycle orchestrator.
    """

    def __init__(self) -> None:
        self._catalog: dict[str, StrategyMeta] = {s.strategy_id: s for s in STRATEGY_CATALOG}
        self._instances: dict[str, BaseStrategy] = {}
        # W24-8 — strategy auto-disable. ``_disabled`` holds strategy_ids
        # that were auto-disabled by the strategy health monitor
        # (``core.strategy_health.StrategyHealthMonitor._disable``) so
        # subsequent ``start_strategy`` calls short-circuit instead of
        # silently restarting a strategy the operator (or the health
        # monitor) explicitly turned off. ``enable()`` clears the flag.
        # Stored separately from ``_instances`` so a strategy can be
        # disabled WITHOUT first being started (the monitor runs even
        # when the strategy hasn't been instantiated yet — e.g. an
        # out-of-sample failure detected before live deployment).
        self._disabled: set[str] = set()
        # W34-3 — per-market pause / close state. ``_paused_markets``
        # holds token_ids for which the pre-submission gate should
        # short-circuit (suspended OR closed markets); ``_closed_markets``
        # holds token_ids that are permanently closed (resolved markets
        # — close_positions_for_market has been called). A closed market
        # is also "paused" (no new orders should land), so
        # ``close_positions_for_market`` adds the token to BOTH sets.
        self._paused_markets: set[str] = set()
        self._closed_markets: set[str] = set()

    def get_catalog(self, implemented_only: bool = False) -> list[dict]:
        """Return the catalog as a list of plain dicts.

        ``implemented_only=True`` filters out PLANNED / EXPERIMENTAL
        entries — used by ``GET /api/strategies/catalog?implemented_only=true``
        so the UI can show only strategies that actually execute.
        """
        rows: list[dict] = []
        for s in STRATEGY_CATALOG:
            if implemented_only and s.status != STATUS_IMPLEMENTED:
                continue
            implemented = _is_implemented(s.strategy_id)
            rows.append({
                "strategy_id": s.strategy_id,
                "name": s.name,
                "category": s.category,
                "description": s.description,
                "risk_level": s.risk_level,
                "status": s.status,
                # ``implemented`` is retained as a backward-compat boolean
                # derived from ``status == IMPLEMENTED``. Older API
                # consumers (and the existing test suite) read this flag;
                # new consumers should prefer ``status``.
                "implemented": implemented,
                "is_running": implemented and (s.strategy_id in self._instances),
                "default_enabled": s.default_enabled,
                # W24-8 — surface the auto-disable flag so the UI can
                # render a "DISABLED — auto-disabled by health monitor"
                # badge on strategies the monitor turned off (vs. a
                # strategy that was simply never started).
                "is_disabled": s.strategy_id in self._disabled,
            })
        return rows

    async def start_strategy(self, strategy_id: str) -> bool:
        # Resolve legacy aliases (``market_maker`` → ``mm_avellaneda_stoikov``).
        canonical_id = _LEGACY_ALIASES.get(strategy_id, strategy_id)
        if canonical_id != strategy_id:
            strategy_id = canonical_id

        # W24-8 — short-circuit if the strategy was auto-disabled by
        # the health monitor. ``enable()`` clears the flag and is the
        # explicit operator path to resume a disabled strategy.
        if strategy_id in self._disabled:
            log.info(
                "[strategy_registry] %s is disabled; skipping start "
                "(call enable() to re-enable)", strategy_id,
            )
            return False

        if strategy_id in self._instances:
            return True
        meta = self._catalog.get(strategy_id)
        if not meta:
            return False

        # If it's one of our IMPLEMENTED strategies, instantiate the
        # concrete class via lazy import (avoids import-time side effects
        # from strategies/market_maker / strategies/arb_scanner / etc.).
        if _is_implemented(strategy_id):
            inst = self._instantiate_implemented(strategy_id)
            if inst is None:
                return False
        else:
            inst = QuantStrategyInstance(meta)

        await inst.start()
        self._instances[strategy_id] = inst
        return True

    def _instantiate_implemented(self, strategy_id: str) -> BaseStrategy | None:
        """Lazily import and instantiate the concrete strategy class."""
        try:
            module_path, class_name = _IMPLEMENTED_STRATEGY_CLASSES[strategy_id].rsplit(".", 1)
            import importlib
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
            return cls()
        except Exception as e:
            log.error("[strategy_registry] Failed to instantiate %s: %s", strategy_id, e)
            return None

    async def stop_strategy(self, strategy_id: str) -> bool:
        # Resolve legacy aliases on the way out too.
        canonical_id = _LEGACY_ALIASES.get(strategy_id, strategy_id)
        if canonical_id != strategy_id:
            strategy_id = canonical_id

        inst = self._instances.pop(strategy_id, None)
        if inst:
            await inst.stop()
            return True
        return False

    def disable(self, strategy_id: str, reason: str = "") -> bool:
        """SYNC auto-disable entry point — marks the strategy as disabled
        AND cancels its running loop without awaiting.

        W24-8 — the strategy health monitor's
        ``StrategyHealthMonitor.check_strategy`` is itself a sync method
        (called from a periodic check loop / health endpoint), so it
        cannot ``await`` the async ``stop_strategy``. This sync wrapper:

        1. Resolves legacy aliases (``market_maker`` → ``mm_avellaneda_stoikov``)
           so a disable request by either name lands on the same row.
        2. Adds the strategy_id to ``self._disabled`` so subsequent
           ``start_strategy`` calls short-circuit (a disabled strategy
           cannot be restarted without ``enable()``).
        3. Pops the running instance (if any) from ``_instances`` and
           cancels its asyncio ``_task`` WITHOUT awaiting — the task's
           ``CancelledError`` will be raised on its next tick and the
           loop will tear it down. ``inst._running`` is set to ``False``
           so any in-flight cycle sees the flag and exits cleanly.
        4. Returns ``True`` iff the strategy was in the catalog (whether
           or not it was actually running) — the caller uses this to
           decide whether the disable was a no-op.

        The optional ``reason`` string is logged at WARNING level so the
        operator can correlate the disable with the health check that
        triggered it. ``reason`` is NOT persisted on the registry itself
        — the canonical record of WHY a strategy was disabled lives on
        the ``StrategyHealth`` dataclass in
        ``core.strategy_health.StrategyHealthMonitor._health`` (and in
        the alert fired via ``alert_engine.record_alert``).

        The async ``stop_strategy`` is still the preferred path when the
        caller has a running event loop — it awaits ``inst.stop()`` which
        awaits ``store.log_event`` so the strategy-stopped event lands in
        the audit trail synchronously. ``disable`` is for the no-loop /
        can't-await case (tests, periodic health checks).
        """
        canonical_id = _LEGACY_ALIASES.get(strategy_id, strategy_id)
        if canonical_id != strategy_id:
            strategy_id = canonical_id

        if strategy_id not in self._catalog:
            return False

        already_disabled = strategy_id in self._disabled
        self._disabled.add(strategy_id)

        inst = self._instances.pop(strategy_id, None)
        if inst is not None:
            try:
                # Mark not running so any in-flight cycle sees the flag.
                inst._running = False
                task = getattr(inst, "_task", None)
                if task is not None and not task.done():
                    task.cancel()
            except Exception as e:  # noqa: BLE001 — defensive: disable must not raise
                log.warning(
                    "[strategy_registry] disable(%s) cleanup error: %s",
                    strategy_id, e,
                )
            log.warning(
                "[strategy_registry] Auto-disabled %s: %s (instance stopped)",
                strategy_id, reason or "(no reason given)",
            )
        elif not already_disabled:
            log.warning(
                "[strategy_registry] Auto-disabled %s: %s (not running)",
                strategy_id, reason or "(no reason given)",
            )
        return True

    def enable(self, strategy_id: str) -> bool:
        """Re-enable a previously auto-disabled strategy.

        W24-8 — counterpart to ``disable()``. Clears the ``_disabled``
        flag so the next ``start_strategy`` call can resume the
        strategy. Does NOT auto-start the strategy — the operator (or
        the supervisor) must explicitly call ``start_strategy`` after
        ``enable()`` so the resume is intentional, not a side-effect of
        clearing the flag.

        Returns ``True`` iff the strategy was previously disabled (i.e.
        the flag actually needed clearing). ``False`` means the
        strategy wasn't disabled to begin with — a no-op, not an error.
        """
        canonical_id = _LEGACY_ALIASES.get(strategy_id, strategy_id)
        if canonical_id != strategy_id:
            strategy_id = canonical_id

        if strategy_id in self._disabled:
            self._disabled.discard(strategy_id)
            log.info("[strategy_registry] Re-enabled %s", strategy_id)
            return True
        return False

    def is_disabled(self, strategy_id: str) -> bool:
        """W24-8 — check whether a strategy is currently auto-disabled."""
        canonical_id = _LEGACY_ALIASES.get(strategy_id, strategy_id)
        return canonical_id in self._disabled

    # ── W34-3 — per-market pause / close state ──────────────────────────────
    # The MarketEventIngester calls these methods on MARKET_SUSPENDED and
    # MARKET_RESOLVED events so the pre-submission gate (a future task)
    # can short-circuit orders for markets that are paused (suspended) or
    # permanently closed (resolved). The state lives on the registry so a
    # single ``is_market_paused(token_id)`` lookup is the canonical gate
    # every call site (paper_sim, live broker, shadow trader) can check.

    def pause_for_market(self, token_id: str) -> None:
        """Mark ``token_id`` as paused (no new orders should land).

        Called by ``ingestion.market_events.MarketEventIngester.record_event``
        on a ``MARKET_SUSPENDED`` event. Idempotent — calling twice with
        the same token_id is a no-op (set semantics). An empty ``token_id``
        is a defensive no-op (the ingester should never call this with an
        empty id, but the guard keeps the contract honest).

        Note: a paused market is NOT closed — a subsequent
        ``MARKET_REOPENED`` event can resume trading by calling
        ``reset_market_state(token_id)``. ``reset_market_state`` is the
        canonical un-pause path; this method only adds to the set.
        """
        if not token_id:
            return
        self._paused_markets.add(token_id)

    def close_positions_for_market(self, token_id: str) -> None:
        """Mark ``token_id`` as permanently closed (resolved market).

        Called by ``ingestion.market_events.MarketEventIngester.record_event``
        on a ``MARKET_RESOLVED`` event. A closed market is also "paused"
        (no new orders should land) so the token is added to BOTH the
        ``_closed_markets`` and ``_paused_markets`` sets. Idempotent —
        calling twice with the same token_id is a no-op. An empty
        ``token_id`` is a defensive no-op.

        Additionally iterates every running strategy instance and clears
        any active orders for the market across the ``_active_orders``
        dict on each instance. Each concrete strategy (e.g.
        ``MarketMakerStrategy`` / ``ArbScannerStrategy``) tracks its
        per-market orders in ``self._active_orders`` so the close call
        can fan out across every running strategy without each strategy
        having to register a per-market callback.
        """
        if not token_id:
            return
        self._closed_markets.add(token_id)
        self._paused_markets.add(token_id)
        # Fan out across every running strategy instance — clear any
        # active orders for this market. ``_active_orders`` is a
        # ``dict[token_id, order_id]`` on each instance, so popping the
        # token_id is the canonical "cancel any open order for this
        # market" signal. Best-effort — a strategy without the
        # ``_active_orders`` attr (or whose attr isn't a dict) is
        # silently skipped (the contract is "best-effort close, never
        # raise").
        for inst in self._instances.values():
            orders = getattr(inst, "_active_orders", None)
            if not isinstance(orders, dict):
                continue
            try:
                orders.pop(token_id, None)
            except Exception as e:  # noqa: BLE001 — defensive: close must never raise
                log.debug(
                    "[strategy_registry] close_positions_for_market(%s) "
                    "instance %s cleanup error: %s",
                    token_id, getattr(inst, "name", "?"), e,
                )

    def reset_market_state(self, token_id: str | None = None) -> None:
        """Clear the per-market pause / close state.

        When ``token_id`` is supplied, clears ONLY that token's state
        (used by ``MARKET_REOPENED`` to resume trading on a previously
        suspended market). When ``token_id`` is ``None``, clears ALL
        market state (used by the test-suite autouse fixture so a prior
        test's pause / close calls don't leak into the next test).
        """
        if token_id is None:
            self._paused_markets.clear()
            self._closed_markets.clear()
            return
        self._paused_markets.discard(token_id)
        self._closed_markets.discard(token_id)

    def is_market_paused(self, token_id: str) -> bool:
        """Return True iff ``token_id`` is paused (suspended or closed)."""
        return token_id in self._paused_markets

    def is_market_closed(self, token_id: str) -> bool:
        """Return True iff ``token_id`` is permanently closed (resolved)."""
        return token_id in self._closed_markets

    def get_active_instances(self) -> dict[str, BaseStrategy]:
        return self._instances


# Global singleton
strategy_registry = StrategyRegistry()
