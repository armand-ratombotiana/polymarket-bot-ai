"""
strategies/event_driven.py — News Sentiment Event-Driven Trader.

W22-3 — implements the unified strategy contract for the second of
five high-value strategies promoted from the PLANNED catalog. Maps
to catalog id ``event_news_sentiment`` — "News Sentiment Breakout —
NLP sentiment scoring on breaking news feeds to trade probability
shifts".

Signal logic
------------
Event-driven trading reacts to exogenous information arrivals — a
breaking news headline, an earnings call transcript, a Fed statement.
When a news event shifts the implied probability of an associated
prediction market by more than a configurable threshold, the strategy
buys (positive sentiment) or sells (negative sentiment) the
corresponding outcome token.

Inputs (via ``market_context``)
-------------------------------
  * ``token_id`` (str) — the prediction-market outcome token to trade.
  * ``mid`` (float ∈ (0, 1)) — the current market mid-price.
  * ``news_event`` (dict, optional) — carries ``headline`` (str),
    ``sentiment_score`` (float ∈ [-1, +1] where -1 = very bearish,
    +1 = very bullish), ``confidence`` (float ∈ [0, 1] = the NLP
    model's confidence in the sentiment classification), and
    ``timestamp`` (float epoch seconds).
  * ``news_velocity`` (float, optional) — the rate of news mentions
    per minute (a spike > 5x baseline triggers the ``breakout`` regime).
  * ``time_since_event_seconds`` (float, optional) — how stale the
    news is. The strategy only acts on news < ``MAX_NEWS_AGE_SECONDS``
    old (default 300 s = 5 min).

Edge estimation
---------------
The expected edge is ``|sentiment_score| × confidence × velocity_multiplier``.
A high-confidence, high-velocity news event moves markets more than
a low-confidence, low-velocity one.

Order routing
-------------
The async ``_run`` loop is intentionally a no-op stub; the strategy
is driven by the sync contract surface (``generate_signal``) for
backtest / dashboard introspection. Live trading wiring (paper /
CLOB order submission via ``BaseStrategy.submit_order``) is left
to a future wave — the strategy is honest about its status.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

# ── Strategy parameters ───────────────────────────────────────────────────────
SENTIMENT_THRESHOLD = 0.40    # |sentiment| ≥ 0.40 ⇒ actionable signal
MIN_CONFIDENCE = 0.50         # NLP model must be ≥ 50% confident
VELOCITY_BREAKOUT_MULT = 5.0  # 5x baseline mentions = "breakout" regime
MAX_NEWS_AGE_SECONDS = 300.0  # 5 min — news older than this is stale
MAX_POSITION_PCT = 0.05        # never risk > 5% of capital on a single event
SCAN_INTERVAL = 30.0


class EventDriven(BaseStrategy):
    """News sentiment breakout trader — acts on NLP-scored news events."""

    name = "event_driven"

    def __init__(self) -> None:
        super().__init__()
        self.sentiment_threshold: float = SENTIMENT_THRESHOLD
        self.min_confidence: float = MIN_CONFIDENCE
        self.velocity_breakout_mult: float = VELOCITY_BREAKOUT_MULT
        self.max_news_age_seconds: float = MAX_NEWS_AGE_SECONDS
        self.max_position_pct: float = MAX_POSITION_PCT
        self._interval: float = SCAN_INTERVAL
        # Per-token cooldown: once we act on a token, don't re-act for
        # ``cooldown_seconds`` so we don't stack on the same news.
        self._last_act_at: dict[str, float] = {}
        self._cooldown_seconds: float = 60.0

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Async scan loop stub.

        W22-3 — the live trading wiring (paper-sim create_order /
        clob_client.create_order) is intentionally deferred; the
        strategy is catalog-IMPLEMENTED so the dashboard surfaces it,
        but its canonical signal surface is the SYNC contract method
        ``generate_signal``. The loop polls and logs so the registry
        lifecycle's ``start`` / ``stop`` plumbing works end-to-end.
        """
        log.info(
            "[event_driven] Active (|sent|≥%.2f, conf≥%.2f, vel×%.1f, age<%.0fs)",
            self.sentiment_threshold,
            self.min_confidence,
            self.velocity_breakout_mult,
            self.max_news_age_seconds,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[event_driven] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    # ── W22-3 — StrategyContract implementations ─────────────────────────────

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "News sentiment breakout trader — acts on NLP-scored "
                "breaking-news events when sentiment shifts exceed the "
                "action threshold."
            ),
            "author": "polymarket-bot",
            "category": "event_driven",
            "model": "nlp_sentiment_breakout",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "sentiment_threshold" in config:
            self.sentiment_threshold = float(config["sentiment_threshold"])
        if "min_confidence" in config:
            self.min_confidence = float(config["min_confidence"])
        if "velocity_breakout_mult" in config:
            self.velocity_breakout_mult = float(config["velocity_breakout_mult"])
        if "max_news_age_seconds" in config:
            self.max_news_age_seconds = float(config["max_news_age_seconds"])
        if "max_position_pct" in config:
            self.max_position_pct = float(config["max_position_pct"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])
        if "cooldown_seconds" in config:
            self._cooldown_seconds = float(config["cooldown_seconds"])

    def validate(self) -> tuple[bool, str]:
        if not 0.0 < self.sentiment_threshold <= 1.0:
            return False, (
                f"sentiment_threshold={self.sentiment_threshold} must be "
                f"in (0, 1]"
            )
        if not 0.0 <= self.min_confidence <= 1.0:
            return False, (
                f"min_confidence={self.min_confidence} must be in [0, 1]"
            )
        if self.velocity_breakout_mult <= 0.0:
            return False, (
                f"velocity_breakout_mult={self.velocity_breakout_mult} "
                f"must be > 0"
            )
        if self.max_news_age_seconds <= 0.0:
            return False, (
                f"max_news_age_seconds={self.max_news_age_seconds} must be > 0"
            )
        if not 0.0 < self.max_position_pct <= 1.0:
            return False, (
                f"max_position_pct={self.max_position_pct} must be in (0, 1]"
            )
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        """Build an event-driven Signal from an NLP-scored news event.

        Returns ``None`` when:
          * no ``news_event`` is provided (no catalyst to trade on),
          * the news is too old (``time_since_event_seconds`` > cap),
          * the NLP confidence is below ``min_confidence``,
          * the |sentiment_score| is below ``sentiment_threshold``,
          * the token is on cooldown from a prior recent act.
        """
        token_id = market_context.get("token_id")
        mid = market_context.get("mid")
        if not token_id or mid is None:
            return None

        news = market_context.get("news_event") or {}
        if not news:
            return None

        sentiment = float(news.get("sentiment_score", 0.0))
        nlp_confidence = float(news.get("confidence", 0.0))
        if nlp_confidence < self.min_confidence:
            return None
        if abs(sentiment) < self.sentiment_threshold:
            return None

        # Stale-news filter — news older than the cap doesn't move markets.
        news_age = float(market_context.get("time_since_event_seconds", 0.0))
        if news_age > self.max_news_age_seconds:
            return None

        # Per-token cooldown — don't re-act on the same token too soon.
        last_act = self._last_act_at.get(token_id, 0.0)
        now = float(market_context.get("now", time.time()))
        if now - last_act < self._cooldown_seconds:
            return None

        # Velocity multiplier — a news velocity spike (≥ 5x baseline)
        # boosts the edge & confidence proportionally.
        velocity = float(market_context.get("news_velocity", 1.0))
        velocity_mult = min(2.0, max(1.0, velocity / self.velocity_breakout_mult))

        # Direction: positive sentiment ⇒ BUY (the market's YES token
        # is underpriced relative to the news), negative ⇒ SELL.
        if sentiment > 0:
            action = "BUY"
            target_price = round(min(0.98, float(mid) + 0.005), 4)
        else:
            action = "SELL"
            target_price = round(max(0.02, float(mid) - 0.005), 4)

        # Edge = |sentiment| × nlp_confidence × velocity_mult — a
        # high-velocity, high-confidence, strong-sentiment event has
        # the biggest expected probability shift. Scaled to ~0-1 range.
        edge = abs(sentiment) * nlp_confidence * velocity_mult / 2.0
        confidence = min(0.95, 0.5 + abs(sentiment) * 0.3 + (velocity_mult - 1.0) * 0.1)

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        self._last_act_at[token_id] = now

        return Signal(
            action=action,
            token_id=token_id,
            size=1.0,  # sized in size_position
            price=target_price,
            confidence=confidence,
            edge=edge,
            reason=(
                f"EventDriven {action}: sentiment={sentiment:+.2f}, "
                f"nlp_conf={nlp_confidence:.2f}, vel={velocity:.1f}x, "
                f"age={news_age:.0f}s"
            ),
            metadata={
                "sentiment_score": sentiment,
                "nlp_confidence": nlp_confidence,
                "news_velocity": velocity,
                "velocity_multiplier": velocity_mult,
                "news_age_seconds": news_age,
                "headline": news.get("headline", "")[:120],
                "model": "nlp_sentiment_breakout",
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        """Edge = expected P&L per dollar (sentiment × conf × velocity)."""
        if signal is None:
            return 0.0
        return signal.edge

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        """Size via edge-scaled fractional sizing capped at max_position_pct.

        Position size = min(max_position_pct × capital, edge × capital × 1.0)
        bounded by available capital and any per-market risk cap. The
        1.0 multiplier approximates full-Kelly on the edge estimate
        (event-driven trades have a tighter time horizon so the sizing
        is more aggressive than stat-arb's 0.5).
        """
        if signal is None or signal.action == "HOLD":
            return 0.0
        max_size = self.max_position_pct * capital
        kelly_size = signal.edge * capital
        risk_cap = float(risk_params.get("max_position_per_market", max_size))
        return min(max_size, kelly_size, risk_cap, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        """Return entry execution params — limit order at signal.price."""
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "signal action is HOLD or None"}
        price = signal.price if signal.price is not None else float(
            market_context.get("mid", 0.5)
        )
        return {
            "token_id": signal.token_id,
            "price": price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "nlp_sentiment_breakout",
                "sentiment_score": signal.metadata.get("sentiment_score"),
                "headline": signal.metadata.get("headline"),
                "news_age_seconds": signal.metadata.get("news_age_seconds"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when the news catalyst has been fully priced in.

        Heuristic: exit when either (a) the position has been open
        longer than ``max_hold_seconds`` (the catalyst's price impact
        has been absorbed), or (b) a follow-up news event with opposite
        sentiment arrives (sentiment reversal signal).
        """
        if not position:
            return None
        held_seconds = float(position.get("held_seconds", 0.0))
        max_hold = float(position.get("max_hold_seconds", 600.0))
        if held_seconds >= max_hold:
            return {
                "reason": "max_hold_seconds reached — catalyst fully priced",
                "held_seconds": held_seconds,
                "type": "market",
            }
        # Sentiment reversal — a follow-up news event with opposite sign.
        news = market_context.get("news_event") or {}
        new_sentiment = float(news.get("sentiment_score", 0.0))
        entry_sentiment = float(position.get("entry_sentiment", 0.0))
        if entry_sentiment != 0.0 and (
            (entry_sentiment > 0 and new_sentiment < -self.sentiment_threshold)
            or (entry_sentiment < 0 and new_sentiment > self.sentiment_threshold)
        ):
            return {
                "reason": "sentiment reversal — opposite-sign news arrived",
                "entry_sentiment": entry_sentiment,
                "new_sentiment": new_sentiment,
                "type": "limit",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "sentiment_threshold": self.sentiment_threshold,
            "min_confidence": self.min_confidence,
            "velocity_breakout_mult": self.velocity_breakout_mult,
            "max_news_age_seconds": self.max_news_age_seconds,
            "cooldown_seconds": self._cooldown_seconds,
            "tokens_in_cooldown": len(self._last_act_at),
        })
        return base
