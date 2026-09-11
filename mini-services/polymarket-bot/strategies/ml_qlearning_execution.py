"""
strategies/ml_qlearning_execution.py — Q-Learning Execution Agent.

W45-1 — implements the unified strategy contract for the
``ml_qlearning_execution`` catalog entry.

Signal logic
------------
Reinforcement learning agent that optimizes limit order placement &
timing. The agent learns a Q-table mapping (state, action) →
expected reward, where:

  States: discretized (mid, spread, time_in_book, position)
  Actions: {JOIN_BID, IMPROVE_BID, CROSS_MARKET, CANCEL, HOLD}
  Reward: realized spread capture per cycle (fill × spread - adverse_selection)

The strategy uses an ε-greedy policy (default ε=0.1) to balance
exploration vs exploitation.

For the contract surface, `generate_signal` returns the greedy
action with highest Q-value (no exploration — that's for training).
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

LEARNING_RATE = 0.1
DISCOUNT_FACTOR = 0.95
EPSILON = 0.1                     # 10% exploration
STATE_BINS = 5                    # discretize each feature into 5 bins
ACTIONS = ("JOIN_BID", "IMPROVE_BID", "CROSS_MARKET", "CANCEL", "HOLD")
SCAN_INTERVAL = 5.0


class QLearningExecutionAgent(BaseStrategy):
    """Q-learning execution optimization agent."""

    name = "ml_qlearning_execution"

    def __init__(self) -> None:
        super().__init__()
        self.learning_rate: float = LEARNING_RATE
        self.discount_factor: float = DISCOUNT_FACTOR
        self.epsilon: float = EPSILON
        self.state_bins: int = STATE_BINS
        self._interval: float = SCAN_INTERVAL
        # Q-table: dict[state_key, dict[action, q_value]]
        self._q_table: dict[str, dict[str, float]] = {}
        self._last_state: dict[str, str] = {}
        self._last_action: dict[str, str] = {}
        self._training_steps: int = 0

    async def _run(self) -> None:
        log.info(
            "[ml_qlearn] Active (lr=%.2f, γ=%.2f, ε=%.2f, bins=%d)",
            self.learning_rate, self.discount_factor, self.epsilon, self.state_bins,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[ml_qlearn] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Q-learning execution agent — RL agent that optimizes limit "
                "order placement & timing via tabular Q-learning with "
                "ε-greedy exploration."
            ),
            "author": "polymarket-bot",
            "category": "machine_learning",
            "model": "qlearning_execution",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("learning_rate", "discount_factor", "epsilon"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "state_bins" in config:
            self.state_bins = int(config["state_bins"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if not 0 < self.learning_rate <= 1:
            return False, "learning_rate must be in (0, 1]"
        if not 0 <= self.discount_factor < 1:
            return False, "discount_factor must be in [0, 1)"
        if not 0 <= self.epsilon <= 1:
            return False, "epsilon must be in [0, 1]"
        if self.state_bins < 2:
            return False, "state_bins must be >= 2"
        return True, "OK"

    def _discretize(self, value: float, low: float, high: float) -> int:
        """Bin a continuous value into [0, state_bins - 1]."""
        if high <= low:
            return 0
        normalized = (value - low) / (high - low)
        binned = int(normalized * self.state_bins)
        return max(0, min(self.state_bins - 1, binned))

    def _state_key(self, market_context: dict) -> str:
        """Discretize market context into a state key string."""
        mid = float(market_context.get("mid", 0.5))
        spread = float(market_context.get("spread", 0.01))
        time_in_book = float(market_context.get("time_in_book_sec", 0.0))
        position = float(market_context.get("inventory", 0.0))
        b_mid = self._discretize(mid, 0.0, 1.0)
        b_spread = self._discretize(spread, 0.0, 0.1)
        b_tib = self._discretize(time_in_book, 0.0, 300.0)
        b_pos = self._discretize(position, -100.0, 100.0)
        return f"{b_mid}_{b_spread}_{b_tib}_{b_pos}"

    def _get_q_values(self, state_key: str) -> dict[str, float]:
        """Return Q-values dict for state_key (initialize if new)."""
        if state_key not in self._q_table:
            self._q_table[state_key] = {a: 0.0 for a in ACTIONS}
        return self._q_table[state_key]

    def _greedy_action(self, state_key: str) -> str:
        q_values = self._get_q_values(state_key)
        # Argmax over actions.
        max_q = max(q_values.values())
        candidates = [a for a, q in q_values.items() if q == max_q]
        return random.choice(candidates) if candidates else "HOLD"

    def _epsilon_greedy_action(self, state_key: str) -> str:
        if random.random() < self.epsilon:
            return random.choice(ACTIONS)
        return self._greedy_action(state_key)

    def _update_q(self, state_key: str, action: str, reward: float,
                  next_state_key: str) -> None:
        """Q-learning update: Q(s,a) ← Q(s,a) + α[r + γ·max_a' Q(s',a') - Q(s,a)]."""
        q_values = self._get_q_values(state_key)
        next_q_values = self._get_q_values(next_state_key)
        max_next_q = max(next_q_values.values()) if next_q_values else 0.0
        td_target = reward + self.discount_factor * max_next_q
        td_error = td_target - q_values[action]
        q_values[action] += self.learning_rate * td_error
        self._training_steps += 1

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        mid = market_context.get("mid")
        if not token_id or mid is None:
            return None
        try:
            mid_f = float(mid)
        except (TypeError, ValueError):
            return None
        if not 0 < mid_f < 1:
            return None

        state_key = self._state_key(market_context)
        # Use greedy action for the contract surface (deterministic).
        action_choice = self._greedy_action(state_key)

        # Online training: if we have a prior state+action and observed
        # reward, update the Q-table.
        prev_state = self._last_state.get(token_id)
        prev_action = self._last_action.get(token_id)
        reward = market_context.get("reward")
        if prev_state is not None and prev_action is not None and reward is not None:
            try:
                self._update_q(prev_state, prev_action, float(reward), state_key)
            except (TypeError, ValueError):
                pass

        # Translate execution action to BUY/SELL/HOLD Signal.
        if action_choice == "JOIN_BID":
            action = "BUY"
            target_price = round(max(market_context.get("best_bid", mid_f - 0.005), 0.01), 4)
            edge = 0.005
        elif action_choice == "IMPROVE_BID":
            action = "BUY"
            best_bid = float(market_context.get("best_bid", mid_f - 0.005))
            target_price = round(min(best_bid + 0.001, 0.98), 4)
            edge = 0.006
        elif action_choice == "CROSS_MARKET":
            # Aggressive — take the ask (BUY) or hit the bid (SELL) based on inventory.
            inventory = float(market_context.get("inventory", 0.0))
            if inventory < 0:
                action = "BUY"
                target_price = round(float(market_context.get("best_ask", mid_f + 0.005)), 4)
            else:
                action = "SELL"
                target_price = round(float(market_context.get("best_bid", mid_f - 0.005)), 4)
            edge = 0.002  # smaller edge for crossing
        elif action_choice == "CANCEL":
            # No new position — signal HOLD so size_position returns 0.
            action = "HOLD"
            target_price = round(mid_f, 4)
            edge = 0.0
        else:  # HOLD
            action = "HOLD"
            target_price = round(mid_f, 4)
            edge = 0.0

        q_values = self._get_q_values(state_key)
        confidence = min(0.85, 0.3 + abs(max(q_values.values())) / 10.0 + self._training_steps / 10000.0)

        # Track for next-cycle Q-update.
        self._last_state[token_id] = state_key
        self._last_action[token_id] = action_choice

        if action == "HOLD":
            # Still emit a HOLD signal so callers know we evaluated.
            self._stats["evaluations"] = self._stats.get("evaluations", 0) + 1
            return Signal(
                action="HOLD",
                token_id=token_id,
                size=0.0,
                price=target_price,
                confidence=confidence,
                edge=edge,
                reason=f"Q-Learning HOLD: action={action_choice}, state={state_key}",
                metadata={
                    "model": "qlearning_execution",
                    "execution_action": action_choice,
                    "state_key": state_key,
                    "q_values": dict(q_values),
                    "training_steps": self._training_steps,
                },
            )

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action=action,
            token_id=token_id,
            size=1.0,
            price=target_price,
            confidence=confidence,
            edge=edge,
            reason=(
                f"Q-Learning {action}: action={action_choice}, state={state_key}, "
                f"max_Q={max(q_values.values()):.3f}, steps={self._training_steps}"
            ),
            metadata={
                "model": "qlearning_execution",
                "execution_action": action_choice,
                "state_key": state_key,
                "q_values": dict(q_values),
                "training_steps": self._training_steps,
                "epsilon": self.epsilon,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        # Size scaled by max Q-value (more confident actions get bigger size).
        q_values = signal.metadata.get("q_values", {})
        max_q = max(q_values.values()) if q_values else 0.0
        size_factor = min(2.5, max(0.5, max_q / 5.0 + 0.5))
        base_size = float(risk_params.get("base_size_usdc", 1.5))
        max_pct = float(risk_params.get("max_position_pct", 0.03))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "Q-learning chose HOLD/CANCEL"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": signal.metadata.get("execution_action") in ("JOIN_BID", "IMPROVE_BID"),
            "metadata": {
                "model": "qlearning_execution",
                "execution_action": signal.metadata.get("execution_action"),
                "state_key": signal.metadata.get("state_key"),
                "q_values": signal.metadata.get("q_values"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when the agent's Q-values indicate CANCEL is optimal."""
        if not position:
            return None
        # If the agent has switched to favoring CANCEL in the current
        # state, exit the position.
        current_q_values = market_context.get("current_q_values", {})
        if not current_q_values:
            return None
        cancel_q = float(current_q_values.get("CANCEL", 0.0))
        max_q = max(current_q_values.values()) if current_q_values else 0.0
        if cancel_q == max_q and cancel_q > 0:
            return {
                "reason": "Q-learning favors CANCEL — exit position",
                "cancel_q": cancel_q,
                "max_q": max_q,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "learning_rate": self.learning_rate,
            "discount_factor": self.discount_factor,
            "epsilon": self.epsilon,
            "q_table_size": len(self._q_table),
            "training_steps": self._training_steps,
        })
        return base
