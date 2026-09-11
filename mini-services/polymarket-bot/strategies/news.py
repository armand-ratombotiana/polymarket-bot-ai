"""
strategies/news.py — Polling-Discrepancy News Trader.

W44-1 — implements the unified strategy contract for the third of
five high-value strategies promoted from the PLANNED catalog in
this wave. Maps to catalog id ``event_poll_discrepancy`` —
originally "Polling Gap Exploiter — Exploits statistical gaps
between real-world polling and market prices".

Signal logic
------------
Prediction-market prices should track polling-derived probability
estimates for politically-relevant markets (elections, referenda,
approval ratings). When the market's mid diverges from the
polling-implied probability by more than the polling margin of error
plus a configurable edge buffer, the strategy trades the gap — BUY
the under-priced outcome (polling-implied > market mid) or SELL the
over-priced outcome (polling-implied < market mid).

Distinct from ``strategies/event_driven.py`` (the W22-3 ``EventDriven``
strategy that maps to ``event_news_sentiment``): EventDriven reacts
to NLP-scored breaking-news headlines; this strategy reacts to
structured polling data (which is a slow-moving form of news). The
two are complementary — EventDriven captures intraday sentiment
shocks; News captures multi-day polling-vs-price dislocations.

Inputs (via ``market_context``)
-------------------------------
  * ``token_id`` (str, required) — the outcome token to trade.
  * ``mid`` (float ∈ (0, 1), required) — current market mid.
  * ``poll_probability`` (float ∈ (0, 1), required) — the polling-
    derived probability that the outcome resolves YES (e.g. 0.52 =
    polling says 52% YES).
  * ``poll_margin_of_error`` (float ∈ (0, 0.1], optional) — the
    polling firm's stated 95% confidence interval (default 0.03 =
    ±3%).
  * ``poll_sample_size`` (int > 0, optional) — the polling sample
    size. Below ``min_sample_size`` the strategy skips (small-sample
    polls are noise).
  * ``poll_freshness_hours`` (float > 0, optional) — how old the
    poll is. Above ``max_poll_age_hours`` the strategy skips.
  * ``spread`` (float > 0, optional) — current bid-ask spread;
    skips ≥ ``max_spread``.

Edge estimation
---------------
Edge = ``|poll_probability - mid| - margin_of_error - min_edge_buffer``.
The polling margin of error is subtracted because a gap inside the
margin of error is statistically indistinguishable from no gap at
all — only gaps OUTSIDE the margin of error are actionable. The
``min_edge_buffer`` covers spread + fees + slippage.

Order routing
-------------
The async ``_run`` loop is intentionally a no-op stub; the strategy
is driven by the sync contract surface (``generate_signal``) for
backtest / dashboard introspection. Live trading wiring (paper /
CLOB order submission via ``BaseStrategy.submit_order``) is left to a
future wave — the strategy is honest about its status.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

# ── Strategy parameters ───────────────────────────────────────────────────────
MIN_EDGE_BUFFER = 0.025          # 2.5% — covers spread + fees + slippage
DEFAULT_MOE = 0.03                # ±3% default poll margin of error
MAX_MOE = 0.10                    # polls with >10% MOE are noise; skip
MIN_SAMPLE_SIZE = 500             # skip polls with < 500 respondents
MAX_POLL_AGE_HOURS = 72.0         # skip polls older than 3 days
MAX_SPREAD = 0.04                # skip markets with ≥ 4% spreads
MAX_POSITION_PCT = 0.05           # 5% of capital per news trade
SCAN_INTERVAL = 60.0


class NewsTrader(BaseStrategy):
    """Polling-discrepancy news trader.

    BUY when ``poll_probability - mid > margin_of_error + min_edge``.
    SELL when ``mid - poll_probability > margin_of_error + min_edge``.
    Skips when the gap is inside the polling margin of error (no
    statistical signal) or when the poll is too small / too stale.
    """

    name = "news_trader"

    def __init__(self) -> None:
        super().__init__()
        self.min_edge_buffer: float = MIN_EDGE_BUFFER
        self.default_moe: float = DEFAULT_MOE
        self.max_moe: float = MAX_MOE
        self.min_sample_size: int = MIN_SAMPLE_SIZE
        self.max_poll_age_hours: float = MAX_POLL_AGE_HOURS
        self.max_spread: float = MAX_SPREAD
        self.max_position_pct: float = MAX_POSITION_PCT
        self._interval: float = SCAN_INTERVAL
        # Per-token cooldown — don't re-act on the same poll too soon.
        self._last_act_at: dict[str, float] = {}
        self._cooldown_seconds: float = 300.0  # 5 min cooldown per token

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Async scan loop stub.

        W44-1 — the live trading wiring (paper-sim create_order /
        clob_client.create_order) is intentionally deferred; the
        strategy is catalog-IMPLEMENTED so the dashboard surfaces it,
        but its canonical signal surface is the SYNC contract method
        ``generate_signal``. The loop polls and logs so the registry
        lifecycle's ``start`` / ``stop`` plumbing works end-to-end.
        """
        log.info(
            "[news_trader] Active (edge>=%.2f%%, moe<=%.2f%%, n>=%d, age<=%.0fh)",
            self.min_edge_buffer * 100, self.max_moe * 100,
            self.min_sample_size, self.max_poll_age_hours,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[news_trader] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    # ── W44-1 — StrategyContract implementations ────────────────────────────

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Polling-discrepancy news trader — trades the gap "
                "between polling-implied probabilities and market "
                "prices for politically-relevant markets."
            ),
            "author": "polymarket-bot",
            "category": "event_driven",
            "model": "polling_discrepancy_breakout",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "min_edge_buffer" in config:
            self.min_edge_buffer = float(config["min_edge_buffer"])
        if "default_moe" in config:
            self.default_moe = float(config["default_moe"])
        if "max_moe" in config:
            self.max_moe = float(config["max_moe"])
        if "min_sample_size" in config:
            self.min_sample_size = int(config["min_sample_size"])
        if "max_poll_age_hours" in config:
            self.max_poll_age_hours = float(config["max_poll_age_hours"])
        if "max_spread" in config:
            self.max_spread = float(config["max_spread"])
        if "max_position_pct" in config:
            self.max_position_pct = float(config["max_position_pct"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])
        if "cooldown_seconds" in config:
            self._cooldown_seconds = float(config["cooldown_seconds"])

    def validate(self) -> tuple[bool, str]:
        if self.min_edge_buffer < 0:
            return False, f"min_edge_buffer={self.min_edge_buffer} must be >= 0"
        if not 0 < self.default_moe <= self.max_moe:
            return False, (
                f"default_moe={self.default_moe} must be in (0, max_moe]"
            )
        if not 0 < self.max_moe <= 0.5:
            return False, f"max_moe={self.max_moe} must be in (0, 0.5]"
        if self.min_sample_size < 1:
            return False, f"min_sample_size={self.min_sample_size} must be >= 1"
        if self.max_poll_age_hours <= 0:
            return False, (
                f"max_poll_age_hours={self.max_poll_age_hours} must be > 0"
            )
        if self.max_spread <= 0:
            return False, f"max_spread={self.max_spread} must be > 0"
        if not 0.0 < self.max_position_pct <= 1.0:
            return False, (
                f"max_position_pct={self.max_position_pct} must be in (0, 1]"
            )
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        """Build a Signal representing a polling-discrepancy trade.

        Returns ``None`` when:
          * required inputs are missing,
          * the poll sample size is below ``min_sample_size``,
          * the poll is older than ``max_poll_age_hours``,
          * the polling MOE exceeds ``max_moe``,
          * the spread is too wide,
          * the gap is inside the polling margin of error + buffer,
          * the token is on cooldown.
        """
        token_id = market_context.get("token_id")
        mid = market_context.get("mid")
        poll_prob = market_context.get("poll_probability")
        if not token_id or mid is None or poll_prob is None:
            return None

        try:
            mid_f = float(mid)
            poll_f = float(poll_prob)
        except (TypeError, ValueError):
            return None

        if not 0.0 < poll_f < 1.0 or not 0.0 < mid_f < 1.0:
            return None

        # Polling-firm regime filters.
        sample = int(market_context.get("poll_sample_size", 0))
        if 0 < sample < self.min_sample_size:
            # Explicit zero (omitted) is allowed — use the default MOE
            # in that case. A non-zero sample below the floor is noise.
            return None
        age_hours = float(market_context.get("poll_freshness_hours", 0.0))
        if age_hours > self.max_poll_age_hours:
            return None
        moe = float(market_context.get("poll_margin_of_error", self.default_moe))
        if moe > self.max_moe:
            return None

        # Spread regime filter.
        spread = float(market_context.get("spread", 0.01))
        if spread >= self.max_spread:
            return None

        # Cooldown.
        now = float(market_context.get("now", time.time()))
        if now - self._last_act_at.get(token_id, 0.0) < self._cooldown_seconds:
            return None

        # Compute the actionable gap: |poll - mid| - moe - buffer.
        # Only gaps OUTSIDE the polling MOE are statistically
        # distinguishable from no gap at all.
        raw_gap = poll_f - mid_f
        action_threshold = moe + self.min_edge_buffer
        if abs(raw_gap) <= action_threshold:
            return None

        # Actionable edge = the portion of the gap that lies outside
        # the margin of error + buffer.
        edge = abs(raw_gap) - action_threshold

        # Direction: BUY when poll > mid (market under-prices YES).
        if raw_gap > 0:
            action = "BUY"
            target_price = round(min(0.99, mid_f + 0.005), 4)
            direction = "long_yes_underpriced"
        else:
            action = "SELL"
            target_price = round(max(0.01, mid_f - 0.005), 4)
            direction = "short_yes_overpriced"

        # Confidence scales with the actionable edge magnitude and
        # the poll sample size (bigger samples ⇒ tighter MOE ⇒ higher
        # confidence the gap is real).
        sample_boost = min(0.20, (sample / 5000.0) * 0.20) if sample > 0 else 0.0
        confidence = min(0.95, 0.55 + abs(raw_gap) * 1.5 + sample_boost)

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
                f"News {action}: poll={poll_f:.3f}, mid={mid_f:.3f}, "
                f"gap={raw_gap * 100:+.2f}%, moe=±{moe * 100:.2f}%, "
                f"edge={edge * 100:.2f}%, n={sample}, age={age_hours:.1f}h"
            ),
            metadata={
                "direction": direction,
                "poll_probability": poll_f,
                "market_mid": mid_f,
                "raw_gap": raw_gap,
                "poll_margin_of_error": moe,
                "poll_sample_size": sample,
                "poll_freshness_hours": age_hours,
                "actionable_edge": edge,
                "model": "polling_discrepancy_breakout",
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        """Edge = expected P&L per dollar at trade entry.

        For news trades, the edge is the actionable portion of the
        gap between the polling-implied probability and the market
        mid (i.e. the part outside the polling margin of error + the
        spread / fee buffer).
        """
        if signal is None:
            return 0.0
        return signal.edge

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        """Size via edge-scaled fractional sizing capped at max_position_pct.

        News trades have moderate expected win rate (the strategy
        only acts on gaps outside the polling MOE + buffer, so the
        statistical edge is real but polls do drift). The 0.5×
        edge multiplier approximates half-Kelly on the actionable
        edge estimate.
        """
        if signal is None or signal.action == "HOLD":
            return 0.0
        max_size = self.max_position_pct * capital
        kelly_size = signal.edge * capital * 0.5
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
                "model": "polling_discrepancy_breakout",
                "poll_probability": signal.metadata.get("poll_probability"),
                "raw_gap": signal.metadata.get("raw_gap"),
                "poll_margin_of_error": signal.metadata.get(
                    "poll_margin_of_error"
                ),
                "poll_sample_size": signal.metadata.get("poll_sample_size"),
                "poll_freshness_hours": signal.metadata.get(
                    "poll_freshness_hours"
                ),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when the gap closes, a fresh poll flips, or the position ages.

        Exit triggers:
          * the gap between poll and mid has closed inside the polling
            MOE (the dislocation has resolved),
          * a fresh poll arrives with the OPPOSITE sign gap (sentiment
            reversal),
          * the position has been open longer than ``max_hold_seconds``
            (polling-driven dislocations typically resolve in days,
            not weeks).
        """
        if not position:
            return None
        held_seconds = float(position.get("held_seconds", 0.0))
        max_hold = float(position.get("max_hold_seconds", 86400.0))  # 24h default
        if held_seconds >= max_hold:
            return {
                "reason": "max_hold_seconds reached — poll gap aged out",
                "held_seconds": held_seconds,
                "type": "market",
            }

        mid = market_context.get("mid")
        poll_prob = market_context.get("poll_probability")
        if mid is None or poll_prob is None:
            return None
        try:
            mid_f = float(mid)
            poll_f = float(poll_prob)
        except (TypeError, ValueError):
            return None

        moe = float(market_context.get("poll_margin_of_error", self.default_moe))
        current_gap = poll_f - mid_f
        entry_gap = float(position.get("entry_gap", 0.0))

        # Gap closed: |current_gap| inside the polling MOE.
        if abs(current_gap) <= moe:
            return {
                "reason": "poll gap closed inside MOE — dislocation resolved",
                "entry_gap": entry_gap,
                "current_gap": current_gap,
                "type": "limit",
            }

        # Sentiment reversal: a fresh poll with opposite-sign gap.
        if entry_gap != 0 and (
            (entry_gap > 0 and current_gap < -moe - self.min_edge_buffer)
            or (entry_gap < 0 and current_gap > moe + self.min_edge_buffer)
        ):
            return {
                "reason": "fresh poll flipped the gap sign — sentiment reversal",
                "entry_gap": entry_gap,
                "current_gap": current_gap,
                "type": "limit",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "min_edge_buffer": self.min_edge_buffer,
            "default_moe": self.default_moe,
            "max_moe": self.max_moe,
            "min_sample_size": self.min_sample_size,
            "max_poll_age_hours": self.max_poll_age_hours,
            "max_spread": self.max_spread,
            "cooldown_seconds": self._cooldown_seconds,
            "tokens_in_cooldown": len(self._last_act_at),
        })
        return base
