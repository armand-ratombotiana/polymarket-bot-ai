"""
risk/routes.py — Risk inspection HTTP surface (paused-strategy visibility).

Additive route registration mirroring the ``register_routes(app)`` pattern
established by ``core/observability.py``, ``core/retention.py``,
``core/live_safety_gate.py``, ``core/capital_allocator.py``,
``core/shadow_trading.py``, and ``ml/routes.py`` — each wired into the
live FastAPI server via a single trailing import + call at the bottom of
``api/server.py``. Per the V12 task contract, ``api/server.py`` is
edited **additively only** (one import + one call appended at end); no
existing route, middleware, decorator, or model is touched.

The endpoint is purely informational / read-only — it never mutates
``risk_manager`` state. In particular it does NOT call
``is_strategy_paused`` (whose lazy-clear contract pops expired entries
on read); doing so from a GET would surprise the next reader with a
mutated dict. Expired cooldowns are filtered out of the response
client-side (``seconds_remaining <= 0``), leaving the lazy-clear
contract to the next ``check_order`` call — exactly as the existing
``risk/manager.py`` design intends.

Endpoint (auth-protected by the caller's existing ``enforce_api_auth``
bearer-token middleware — this path is NOT in ``PUBLIC_PATHS``):

  GET /api/risk/strategies/paused
      Return currently paused strategies (those in per-trade-loss
      cooldown via ``risk_manager._strategy_cooldowns``) with
      ``seconds_remaining``, plus the set of registered-running
      strategies that are NOT currently paused.

      Response shape::

          {
            "paused": [
              {"strategy": "signal_trader", "seconds_remaining": 287.4},
              ...
            ],
            "active": [
              {"strategy": "market_maker"},
              ...
            ],
            "cooldown_seconds": 300.0,
            "threshold_usd": 0.50
          }

      - ``paused`` is sorted by ``seconds_remaining`` descending so the
        strategy with the longest remaining cooldown (most recently
        paused, longest to recover) is first — the one operators most
        need to see. Expired entries (``seconds_remaining <= 0``) are
        filtered out so the endpoint never reports a "paused" strategy
        whose cooldown has already elapsed.
      - ``active`` is the set of strategies currently running per
        ``strategy_registry.get_active_instances()`` that are NOT in the
        paused set. A strategy can appear in ``paused`` without being in
        ``active`` (an ad-hoc strategy name from
        ``report_trade_pnl`` that was never registered); it can also be
        in ``active`` without being in ``paused`` (a registered running
        strategy that hasn't tripped the per-trade breaker). Sorted by
        strategy name for deterministic output.
      - ``cooldown_seconds`` and ``threshold_usd`` are included as
        operational context: the configured
        ``STRATEGY_COOLDOWN`` (seconds) and ``PER_TRADE_MAX_LOSS``
        (USD) constants from ``risk/manager.py`` that govern when a
        strategy enters cooldown. Lets the operator compute
        "fraction of cooldown elapsed" without a second round-trip.

Design notes
------------
- **Read-only / non-mutating**: this is a GET endpoint; the snapshot
  reads ``risk_manager._strategy_cooldowns.items()`` directly without
  calling ``is_strategy_paused`` (which would pop expired entries under
  its lazy-clear contract). Expired entries are filtered out of the
  response client-side, leaving the dict mutation to the next
  ``check_order`` call.
- **Spec / code naming divergence**: the V12 task spec asks for paused
  strategies "from ``risk_manager._paused_strategies`` (or equivalent)".
  The actual attribute on :class:`InstitutionalRiskEngine` is named
  ``_strategy_cooldowns`` (a ``dict[str, float]`` mapping strategy name
  → monotonic-clock timestamp at which its cooldown expires). The
  ``(or equivalent)`` qualifier in the spec covers this naming
  divergence; the data source is identical and the snapshot semantics
  are unchanged. The divergence is documented in the worklog under V12.
- **Defensive on ``strategy_registry``**: the registry import is local
  to ``_active_strategies_snapshot`` so a transient import error (e.g.
  unit-test stub with no strategies module present) degrades gracefully
  to an empty ``active`` list rather than 500-ing the whole endpoint.
  The paused list is computed independently of the registry, so a
  registry hiccup never hides a paused strategy.
- **Async endpoint, sync body**: the body is pure dict comprehension +
  ``time.monotonic()`` arithmetic — no I/O, no awaits. The handler is
  declared ``async def`` to match the convention used by every other
  route in this codebase (FastAPI tolerates either, but consistency
  with ``ml/routes.py`` / ``core/retention.py`` / ``core/live_safety_gate.py``
  is preferable to a one-off ``def``).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from risk.manager import (
    PER_TRADE_MAX_LOSS,
    STRATEGY_COOLDOWN,
    risk_manager,
)

log = logging.getLogger(__name__)


def _paused_strategies_snapshot() -> list[dict[str, Any]]:
    """
    Return a sorted list of ``{strategy, seconds_remaining}`` entries for
    every strategy currently in its per-trade-loss cooldown window.

    Reads ``risk_manager._strategy_cooldowns`` directly (the V12 spec's
    ``_paused_strategies`` equivalent — see the module docstring's
    "Spec / code naming divergence" note). Expired entries
    (``seconds_remaining <= 0``) are filtered out so the endpoint never
    reports a "paused" strategy whose cooldown has already elapsed.

    The filtering is read-only — expired entries are NOT popped here.
    The ``is_strategy_paused`` lazy-clear contract (in ``risk/manager.py``)
    owns the mutation; calling it from a GET would mutate shared state
    and surprise the next ``check_order`` reader.

    Sorting by ``seconds_remaining`` descending puts the strategy with
    the longest remaining cooldown first — the one operators most need
    to see (most recently paused, longest to recover).
    """
    now = time.monotonic()
    paused: list[dict[str, Any]] = []
    for strategy, cooldown_until in risk_manager._strategy_cooldowns.items():
        remaining = max(float(cooldown_until) - now, 0.0)
        if remaining <= 0.0:
            continue
        paused.append(
            {
                "strategy": str(strategy),
                "seconds_remaining": round(remaining, 1),
            }
        )
    paused.sort(key=lambda e: e["seconds_remaining"], reverse=True)
    return paused


def _active_strategies_snapshot(paused_names: set[str]) -> list[dict[str, Any]]:
    """
    Return the registered-running strategies that are NOT currently paused.

    Source: ``strategy_registry.get_active_instances()`` (the live set of
    running strategy instances, regardless of catalog size). A strategy is
    "active" here iff it is registered AND running AND not in the paused
    set computed by ``_paused_strategies_snapshot``.

    The registry import is local so a transient import error (e.g. a
    unit-test stub with no strategies module present) degrades gracefully
    to an empty list rather than 500-ing the whole endpoint. The paused
    list is computed independently of the registry, so a registry hiccup
    never hides a paused strategy.

    Sorted by strategy name for stable, deterministic output across calls.
    """
    try:
        from strategies.registry import strategy_registry

        running = strategy_registry.get_active_instances()
    except Exception as e:  # noqa: BLE001 — defensive: registry is best-effort
        log.warning(
            "[risk.routes] strategy_registry unavailable while computing "
            "active list (returning empty active[]): %s",
            e,
        )
        return []

    active: list[dict[str, Any]] = []
    for strategy_id in running.keys():
        if strategy_id in paused_names:
            continue
        active.append({"strategy": str(strategy_id)})
    active.sort(key=lambda e: e["strategy"])
    return active


def register_routes(app: Any) -> None:
    """
    Append the risk-inspection endpoints to a FastAPI app.

    Pure addition — does not touch any existing route, middleware, or
    decorator. Auth is enforced by the caller's existing
    ``enforce_api_auth`` middleware (none of these paths are in
    ``PUBLIC_PATHS``).

    Endpoints registered:

      GET /api/risk/strategies/paused
          Returns the currently paused (cooldown) strategies with
          ``seconds_remaining`` plus the registered-running strategies
          that are NOT currently paused. See the module docstring for
          the full response shape.

      POST /api/risk/pre-submission-check
          Run the 14-check pre-submission risk gate WITHOUT actually
          submitting an order. Returns the full ``PreSubmissionResult``
          (``approved`` + per-check details) so a caller can dry-run
          an order against the gate before committing. W24-3 — God Mode
          §pre-submission-gate.
    """
    @app.get("/api/risk/strategies/paused", tags=["risk"])
    async def _list_paused_strategies():
        """Return currently paused (cooldown) strategies + active strategies."""
        paused = _paused_strategies_snapshot()
        paused_names = {p["strategy"] for p in paused}
        active = _active_strategies_snapshot(paused_names)
        return {
            "paused": paused,
            "active": active,
            # Operational context — lets operators compute "fraction of
            # cooldown elapsed" without a second round-trip. Mirrors the
            # convention in ``risk/manager.status_report`` of returning
            # both the live value AND the configured limit in the same
            # payload.
            "cooldown_seconds": float(STRATEGY_COOLDOWN),
            "threshold_usd": float(PER_TRADE_MAX_LOSS),
        }

    @app.post("/api/risk/pre-submission-check", tags=["risk"])
    async def _pre_submission_check(
        order_request: dict,
        market_data: dict | None = None,
        account_state: dict | None = None,
    ):
        """Run the pre-submission risk gate WITHOUT submitting the order.

        W24-3 — God Mode §pre-submission-gate. Returns the full
        ``PreSubmissionResult`` (``approved``, per-check ``checks[]``,
        ``rejection_reason``, ``rejection_category``, ``timestamp``) so
        a caller can dry-run an order against the 14-check gate before
        committing to ``submit_order``.

        The endpoint runs the SAME gate ``BaseStrategy.submit_order``
        runs on every order — so a 200 + ``approved: true`` here means
        the order would pass the pre-submission gate at submission time
        (assuming the same context is supplied then).

        Body:
            ``order_request`` (JSON object): ``{token_id, side, size,
            price, strategy, edge?, confidence?, order_id?}``. The
            ``edge`` / ``confidence`` keys are optional — when absent,
            the corresponding checks are skipped (passed=True,
            message="skipped — no input data").

        Query params (or JSON body keys alongside ``order_request``):
            ``market_data`` (JSON object, optional): ``{best_bid,
            best_ask, spread, liquidity, last_update}``. When absent,
            the freshness / spread / liquidity checks are skipped.
            ``account_state`` (JSON object, optional): ``{balance,
            total_exposure, open_orders, daily_pnl, drawdown,
            max_total_exposure, max_single_position, max_open_orders,
            daily_loss_limit, max_drawdown_limit}``. When absent, the
            balance / exposure / single-position / open-orders /
            daily-loss / drawdown checks are skipped.

        Returns:
            The ``PreSubmissionResult`` serialised as a dict —
            ``{approved, checks[], rejection_reason, rejection_category,
            timestamp}``. Each entry in ``checks[]`` carries
            ``{check_name, passed, value, threshold, message}``.

        Auth enforced by ``enforce_api_auth`` (path NOT in
        ``PUBLIC_PATHS``).
        """
        # Late import so the route module loads even if the gate module
        # is mid-refactor (mirrors the local-import pattern used by
        # every other additive route in this codebase).
        from core.pre_submission_gate import pre_submission_gate

        result = pre_submission_gate.check(
            order_request=order_request,
            market_data=market_data,
            account_state=account_state,
        )
        return result.to_dict()


__all__ = ["register_routes"]
