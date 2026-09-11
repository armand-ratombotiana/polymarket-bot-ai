"""
strategies/sentiment.py — Aggregate Social Sentiment Trader.

W44-1 — implements the unified strategy contract for the fourth of
five high-value strategies promoted from the PLANNED catalog in
this wave. Maps to catalog id ``event_social_volume`` — originally
"Social Volume Spike — Detects sudden surges in social mention
velocity to trade news early".

Signal logic
------------
The strategy aggregates social-sentiment signals across N sources
(Twitter, Reddit, Discord, Telegram) per outcome token and maintains
a rolling baseline. A signal fires when the aggregated sentiment
shifts sharply from its rolling baseline in a single direction —
either a sustained positive shift (BUY) or a sustained negative
shift (SELL). The strategy uses a z-score of the current sentiment
relative to its rolling mean to filter out noise and only act on
statistically significant shifts.

Distinct from ``strategies/event_driven.py`` (the W22-3 ``EventDriven``
strategy that maps to ``event_news_sentiment``): EventDriven acts on
individual NLP-scored news headlines; this strategy aggregates
cross-platform social sentiment into a rolling baseline and acts on
sustained deviations. The two are complementary — EventDriven
captures intraday headline shocks; Sentiment captures multi-day
social-sentiment regime shifts.

Inputs (via ``market_context``)
-------------------------------
  * ``token_id`` (str, required) — the outcome token to trade.
  * ``mid`` (float ∈ (0, 1), required) — current market mid.
  * ``current_sentiment`` (float ∈ [-1, +1], required) — the
    aggregated social sentiment score across all sources for this
    token at the current cycle (-1 = very bearish, +1 = very
    bullish, 0 = neutral).
  * ``mention_count`` (int > 0, optional) — total social mentions
    in the aggregation window. Below ``min_mentions`` the strategy
    skips (too few mentions = noise).
  * ``baseline_sentiment`` (float ∈ [-1, +1], optional) — the
    rolling-mean sentiment for this token over the lookback window.
    When omitted, the strategy uses its own per-token rolling mean.
  * ``baseline_std`` (float > 0, optional) — the rolling standard
    deviation of sentiment for this token. When omitted, the
    strategy uses its own per-token rolling σ.
  * ``source_count`` (int > 0, optional) — the number of distinct
    social platforms that contributed to ``current_sentiment``.
    Below ``min_source_count`` the strategy skips (single-source
    sentiment is unreliable).
  * ``spread`` (float > 0, optional) — current bid-ask spread;
    skips ≥ ``max_spread``.

Edge estimation
---------------
Edge = ``|z_score| × confidence × source_diversity_bonus`` where
``z_score = (current - baseline) / baseline_std`` and
``source_diversity_bonus = min(2.0, source_count / 2.0)`` — a
multi-platform sentiment shift is more reliable than a single-
platform one.

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
from collections import OrderedDict, deque
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

# ── Strategy parameters ───────────────────────────────────────────────────────
Z_SCORE_THRESHOLD = 2.0          # |z| ≥ 2.0σ ⇒ statistically significant shift
BASELINE_WINDOW = 50              # 50-cycle rolling baseline (mean + σ)
MIN_MENTIONS = 100                # need ≥ 100 mentions to act
MIN_SOURCE_COUNT = 2              # need ≥ 2 distinct social platforms
MIN_SENTIMENT_MAGNITUDE = 0.15   # |current_sentiment| ≥ 0.15 (skip pure noise)
MAX_SPREAD = 0.05                 # skip markets with ≥ 5% spreads
MAX_POSITION_PCT = 0.04           # 4% of capital per sentiment trade
BASELINE_TOKEN_CAP = 200          # bound the number of tracked tokens
SCAN_INTERVAL = 60.0


class SentimentAggregator(BaseStrategy):
    """Aggregate social sentiment trader.

    Maintains a rolling baseline of social sentiment per token and
    fires BUY/SELL signals when the current aggregated sentiment
    shifts by ≥ ``z_score_threshold`` σ from its baseline, weighted
    by source diversity.
    """

    name = "sentiment_aggregator"

    def __init__(self) -> None:
        super().__init__()
        self.z_score_threshold: float = Z_SCORE_THRESHOLD
        self.baseline_window: int = BASELINE_WINDOW
        self.min_mentions: int = MIN_MENTIONS
        self.min_source_count: int = MIN_SOURCE_COUNT
        self.min_sentiment_magnitude: float = MIN_SENTIMENT_MAGNITUDE
        self.max_spread: float = MAX_SPREAD
        self.max_position_pct: float = MAX_POSITION_PCT
        self._interval: float = SCAN_INTERVAL
        # Per-token rolling sentiment history (for the baseline mean/σ).
        self._sentiment_history: "OrderedDict[str, deque[float]]" = OrderedDict()
        # Per-token cooldown.
        self._last_act_at: dict[str, float] = {}
        self._cooldown_seconds: float = 180.0  # 3 min cooldown per token

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
            "[sentiment] Active (|z|>=%.1fσ, n>=%d, sources>=%d)",
            self.z_score_threshold, self.min_mentions, self.min_source_count,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[sentiment] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    # ── W44-1 — StrategyContract implementations ────────────────────────────

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Aggregate social sentiment trader — maintains a "
                "rolling per-token sentiment baseline and trades "
                "sustained statistically-significant sentiment shifts "
                "across multiple social platforms."
            ),
            "author": "polymarket-bot",
            "category": "event_driven",
            "model": "rolling_zscore_sentiment_breakout",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "z_score_threshold" in config:
            self.z_score_threshold = float(config["z_score_threshold"])
        if "baseline_window" in config:
            self.baseline_window = int(config["baseline_window"])
        if "min_mentions" in config:
            self.min_mentions = int(config["min_mentions"])
        if "min_source_count" in config:
            self.min_source_count = int(config["min_source_count"])
        if "min_sentiment_magnitude" in config:
            self.min_sentiment_magnitude = float(config["min_sentiment_magnitude"])
        if "max_spread" in config:
            self.max_spread = float(config["max_spread"])
        if "max_position_pct" in config:
            self.max_position_pct = float(config["max_position_pct"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])
        if "cooldown_seconds" in config:
            self._cooldown_seconds = float(config["cooldown_seconds"])

    def validate(self) -> tuple[bool, str]:
        if self.z_score_threshold <= 0:
            return False, (
                f"z_score_threshold={self.z_score_threshold} must be > 0"
            )
        if self.baseline_window < 2:
            return False, (
                f"baseline_window={self.baseline_window} must be >= 2"
            )
        if self.min_mentions < 1:
            return False, f"min_mentions={self.min_mentions} must be >= 1"
        if self.min_source_count < 1:
            return False, f"min_source_count={self.min_source_count} must be >= 1"
        if not 0.0 <= self.min_sentiment_magnitude <= 1.0:
            return False, (
                f"min_sentiment_magnitude={self.min_sentiment_magnitude} "
                f"must be in [0, 1]"
            )
        if self.max_spread <= 0:
            return False, f"max_spread={self.max_spread} must be > 0"
        if not 0.0 < self.max_position_pct <= 1.0:
            return False, (
                f"max_position_pct={self.max_position_pct} must be in (0, 1]"
            )
        return True, "OK"

    # ── Rolling baseline (internal) ─────────────────────────────────────────

    def _update_history(self, token_id: str, sentiment: float) -> list[float]:
        """Append ``sentiment`` to the rolling history for ``token_id``.

        Returns the in-memory history as a plain list (the caller
        needs slicing on it; converting once avoids leaking the deque).
        Bounds the token set at ``BASELINE_TOKEN_CAP`` so a 10 000-
        market catalog can't OOM us.
        """
        hist = self._sentiment_history.get(token_id)
        if hist is None:
            hist = deque(maxlen=self.baseline_window)
            self._sentiment_history[token_id] = hist
            if len(self._sentiment_history) > BASELINE_TOKEN_CAP:
                self._sentiment_history.popitem(last=False)
        hist.append(sentiment)
        return list(hist)

    @staticmethod
    def _mean_std(values: list[float]) -> tuple[float, float]:
        """Compute mean + population σ for a non-empty list of values.

        Returns (0.0, 0.0) for an empty list. Population σ (rather
        than sample σ) is used because the rolling baseline is the
        full observed distribution, not a sample from a larger one.
        """
        n = len(values)
        if n == 0:
            return 0.0, 0.0
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        return mean, variance ** 0.5

    # ── W44-1 — StrategyContract implementations (continued) ───────────────

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        """Build a Signal representing a social-sentiment shift trade.

        Returns ``None`` when:
          * required inputs are missing,
          * the spread is too wide,
          * the mention count or source-count floors aren't met,
          * the current sentiment magnitude is below the noise floor,
          * the rolling baseline is too short to compute σ,
          * σ is zero (no historical variance — can't z-score),
          * the z-score is inside the action band,
          * the token is on cooldown.
        """
        token_id = market_context.get("token_id")
        mid = market_context.get("mid")
        current_sentiment = market_context.get("current_sentiment")
        if not token_id or mid is None or current_sentiment is None:
            return None

        try:
            mid_f = float(mid)
            current = float(current_sentiment)
        except (TypeError, ValueError):
            return None

        if not -1.0 <= current <= 1.0:
            return None

        # Spread regime filter.
        spread = float(market_context.get("spread", 0.01))
        if spread >= self.max_spread:
            return None

        # Mention-count floor.
        mentions = int(market_context.get("mention_count", 0))
        if 0 < mentions < self.min_mentions:
            return None
        # An explicit zero (omitted) is allowed — assume the upstream
        # aggregator already enforced the floor.

        # Source-diversity floor.
        sources = int(market_context.get("source_count", 0))
        if 0 < sources < self.min_source_count:
            return None

        # Noise floor: pure-zero sentiment is never actionable.
        if abs(current) < self.min_sentiment_magnitude:
            return None

        # Cooldown.
        now = float(market_context.get("now", time.time()))
        if now - self._last_act_at.get(token_id, 0.0) < self._cooldown_seconds:
            return None

        # Rolling baseline: prefer the caller-supplied baseline
        # (computed upstream by a longer-history aggregator) but fall
        # back to the per-token rolling history if absent.
        baseline_mean = market_context.get("baseline_sentiment")
        baseline_std = market_context.get("baseline_std")
        if baseline_mean is None or baseline_std is None:
            history = self._update_history(token_id, current)
            # Use the rolling history EXCLUDING the current sample so
            # the z-score is computed against the prior baseline.
            prior = history[:-1] if len(history) > 1 else []
            if len(prior) < 2:
                return None  # not enough history to z-score
            baseline_mean, baseline_std = self._mean_std(prior)
            # Update the in-memory history only after using the prior.
        else:
            try:
                baseline_mean = float(baseline_mean)
                baseline_std = float(baseline_std)
            except (TypeError, ValueError):
                return None
            # Still update the in-memory rolling history for future
            # cycles where the caller doesn't supply a baseline.
            self._update_history(token_id, current)

        if baseline_std < 1e-6:
            return None  # zero variance — can't z-score

        z_score = (current - baseline_mean) / baseline_std
        if abs(z_score) < self.z_score_threshold:
            return None  # inside the action band — no significant shift

        # Direction: positive z ⇒ BUY (sentiment shifted bullish), negative
        # ⇒ SELL (sentiment shifted bearish).
        if z_score > 0:
            action = "BUY"
            target_price = round(min(0.99, mid_f + 0.005), 4)
            direction = "long_bullish_shift"
        else:
            action = "SELL"
            target_price = round(max(0.01, mid_f - 0.005), 4)
            direction = "short_bearish_shift"

        # Source-diversity bonus: a multi-platform sentiment shift is
        # more reliable than a single-platform one. Caps at 2.0×.
        source_bonus = min(2.0, max(1.0, sources / 2.0)) if sources > 0 else 1.0
        # Edge = |z| × |current| × source_bonus / 10 (scaled to ~0-1).
        edge = abs(z_score) * abs(current) * source_bonus / 10.0
        # Confidence scales with |z| and source diversity.
        confidence = min(0.92, 0.5 + abs(z_score) * 0.08 + (source_bonus - 1.0) * 0.2)

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
                f"Sentiment {action}: z={z_score:+.2f}σ, current={current:+.2f}, "
                f"baseline={baseline_mean:+.2f}±{baseline_std:.2f}, "
                f"sources={sources}, mentions={mentions}"
            ),
            metadata={
                "direction": direction,
                "z_score": z_score,
                "current_sentiment": current,
                "baseline_mean": baseline_mean,
                "baseline_std": baseline_std,
                "source_count": sources,
                "mention_count": mentions,
                "source_diversity_bonus": source_bonus,
                "model": "rolling_zscore_sentiment_breakout",
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        """Edge = expected P&L per dollar (|z| × |sentiment| × diversity / 10)."""
        if signal is None:
            return 0.0
        return signal.edge

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        """Size via edge-scaled fractional sizing capped at max_position_pct.

        Sentiment trades have moderate expected win rate (the strategy
        only acts on |z| ≥ 2σ shifts with ≥ 2 source platforms and ≥
        100 mentions, so the statistical edge is real but social
        sentiment is notoriously noisy). The 0.5× edge multiplier
        approximates half-Kelly on the edge estimate.
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
                "model": "rolling_zscore_sentiment_breakout",
                "z_score": signal.metadata.get("z_score"),
                "current_sentiment": signal.metadata.get("current_sentiment"),
                "baseline_mean": signal.metadata.get("baseline_mean"),
                "source_count": signal.metadata.get("source_count"),
                "mention_count": signal.metadata.get("mention_count"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when sentiment reverts to baseline or position ages out.

        Exit triggers:
          * the current z-score has reverted inside ±0.5σ (the
            sentiment shift has been absorbed by the market),
          * a fresh sentiment reading flips the sign (sentiment
            reversal),
          * the position has been open longer than ``max_hold_seconds``
            (sentiment-driven dislocations typically resolve in
            hours, not days).
        """
        if not position:
            return None
        held_seconds = float(position.get("held_seconds", 0.0))
        max_hold = float(position.get("max_hold_seconds", 3600.0))
        if held_seconds >= max_hold:
            return {
                "reason": "max_hold_seconds reached — sentiment aged out",
                "held_seconds": held_seconds,
                "type": "market",
            }

        current = market_context.get("current_sentiment")
        if current is None:
            return None
        try:
            current_f = float(current)
        except (TypeError, ValueError):
            return None

        baseline_mean = float(market_context.get(
            "baseline_sentiment", position.get("entry_baseline_mean", 0.0)
        ))
        baseline_std = float(market_context.get(
            "baseline_std", position.get("entry_baseline_std", 1.0)
        ))
        if baseline_std < 1e-6:
            return None

        current_z = (current_f - baseline_mean) / baseline_std
        entry_z = float(position.get("entry_z", 0.0))

        # Reversion: z has reverted to inside ±0.5σ.
        if abs(current_z) <= 0.5:
            return {
                "reason": "sentiment reverted inside ±0.5σ — absorbed",
                "entry_z": entry_z,
                "current_z": current_z,
                "type": "limit",
            }

        # Reversal: sign flipped (bullish → bearish or vice versa).
        if entry_z != 0 and (
            (entry_z > 0 and current_z < -self.z_score_threshold)
            or (entry_z < 0 and current_z > self.z_score_threshold)
        ):
            return {
                "reason": "sentiment sign flipped — reversal",
                "entry_z": entry_z,
                "current_z": current_z,
                "type": "limit",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "z_score_threshold": self.z_score_threshold,
            "baseline_window": self.baseline_window,
            "min_mentions": self.min_mentions,
            "min_source_count": self.min_source_count,
            "min_sentiment_magnitude": self.min_sentiment_magnitude,
            "max_spread": self.max_spread,
            "cooldown_seconds": self._cooldown_seconds,
            "tracked_tokens": len(self._sentiment_history),
            "tokens_in_cooldown": len(self._last_act_at),
        })
        return base
