"""
core/pre_submission_gate.py — Pre-submission risk gate.

W24-3 — God Mode pre-submission risk enforcement.

The God Mode assessment found that several risk controls exist only in
config / UI but aren't enforced by execution: a strategy could submit an
order with insufficient balance, a stale market quote, a duplicate signal,
or while a circuit breaker was open, and the existing risk gate
(``risk/manager.InstitutionalRiskEngine.check_order``) would not catch it
because that gate operates on the in-memory ``store`` snapshot — not on
the live order-book / account / circuit-breaker state.

This module is the FINAL gate before an order reaches the exchange. No
order can bypass it. ``BaseStrategy.submit_order`` calls
``pre_submission_gate.check(...)`` BEFORE the existing
``risk_manager.check_order`` so a gate-rejected order never even reaches
the risk engine (defense in depth — the existing risk engine remains
the second gate; this gate is the first).

Checks (all must pass)
----------------------
  1.  Kill switch not active (durable file-backed marker)
  2.  Sufficient balance for the order cost
  3.  Max total exposure not exceeded (existing exposure + new cost)
  4.  Max single position not exceeded (new order cost alone)
  5.  Max open orders not exceeded
  6.  Daily loss limit not exceeded (P&L > -limit)
  7.  Drawdown within limit (current drawdown < max)
  8.  Market data is fresh (last_update within staleness window)
  9.  Spread within acceptable range (best_ask - best_bid <= max_spread)
  10. Liquidity sufficient (book depth >= min_liquidity)
  11. Edge meets minimum threshold (signal edge >= min_edge)
  12. Confidence meets minimum threshold (signal confidence >= min_confidence)
  13. Idempotency check (not a duplicate of a recent signal)
  14. Circuit breaker not open (CLOB / Gamma)

Permissive defaults
-------------------
When ``account_state`` / ``market_data`` are not provided (the default
for backward-compatible callers that haven't been migrated to pass
context), the account-state and market-data checks return
``passed=True`` with ``message="skipped — no input data"`` rather than
failing the gate. This is the documented contract:

  - The gate ALWAYS runs every check (so the audit trail records every
    check, even the skipped ones).
  - When a check's input data is absent, the check is recorded as
    PASSED with the explicit "skipped" message — operators can see at
    a glance which checks were skipped and which were enforced.
  - Production callers (``strategies/base.submit_order``,
    ``POST /api/risk/pre-submission-check``) SHOULD pass full context
    for full enforcement. When they do, every check is enforced.

The kill-switch, idempotency, and circuit-breaker checks ALWAYS run
regardless of context — they don't depend on caller-supplied data.

Wiring
------
``BaseStrategy.submit_order`` calls ``pre_submission_gate.check(...)``
as the first action inside its body, BEFORE the existing
``risk_manager.check_order`` call. When the gate rejects, the order is
short-circuited (returns ``None``) and the rejection is recorded in the
``rejected_opportunities`` store (fire-and-forget async) so the operator
dashboard can surface "what the gate rejected and why" in the same
analytics roll-up that surfaces risk-engine rejections.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Result dataclasses ──────────────────────────────────────────────────────

@dataclass
class RiskCheckResult:
    """Result of a single risk check (one of the 14 the gate runs)."""

    check_name: str
    passed: bool
    value: Any
    threshold: Any
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_name": self.check_name,
            "passed": self.passed,
            "value": self.value,
            "threshold": self.threshold,
            "message": self.message,
        }


@dataclass
class PreSubmissionResult:
    """Aggregate result of the 14-check pre-submission gate.

    ``approved`` is True ONLY when every check passed (or was
    legitimately skipped due to absent input data). When False, the
    first failing check's ``check_name`` and ``message`` are surfaced
    in ``rejection_reason`` / ``rejection_category`` so the caller
    can record a structured rejection in the rejected-opportunities
    store.
    """

    approved: bool
    checks: list[RiskCheckResult]
    rejection_reason: str = ""
    rejection_category: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "checks": [c.to_dict() for c in self.checks],
            "rejection_reason": self.rejection_reason,
            "rejection_category": self.rejection_category,
            "timestamp": self.timestamp,
        }

    def __dict__(self) -> dict[str, Any]:  # noqa: D401 — convenience alias
        # ``BaseStrategy.submit_order``'s spec wiring uses
        # ``result.__dict__`` directly; provide it as a forwarder to
        # ``to_dict`` so the API route returns the structured shape
        # (the raw dataclass ``__dict__`` would emit ``checks`` as a
        # list of ``RiskCheckResult`` instances, not dicts).
        return self.to_dict()


# ── Gate ───────────────────────────────────────────────────────────────────

# Sentinel for "no input data was provided" — recorded as a passed check
# with this message so the audit trail distinguishes "passed because the
# check ran and the value was OK" from "skipped because no data was
# provided". Operators reading the audit trail can then immediately see
# which checks were actually enforced.
_SKIPPED_MSG = "skipped — no input data"


class PreSubmissionGate:
    """Final risk gate before order submission.

    A single process-wide singleton (``pre_submission_gate``) is
    constructed at module-import time. ``BaseStrategy.submit_order``,
    the API route, and tests all share one gate so the configured
    thresholds are consistent across every call site.
    """

    def __init__(self) -> None:
        # Thresholds — conservative defaults. Operators can tune via the
        # ``configure`` method (used by tests; future: from ``settings``).
        self._min_edge: float = 0.03          # 3% minimum predicted edge
        self._min_confidence: float = 0.55   # ML confidence floor
        self._max_spread: float = 0.10        # 10% max bid-ask spread
        self._min_liquidity: float = 50.0     # $50 min book depth
        self._max_staleness_seconds: float = 60.0

    # ── Configuration ────────────────────────────────────────────────────

    def configure(
        self,
        *,
        min_edge: Optional[float] = None,
        min_confidence: Optional[float] = None,
        max_spread: Optional[float] = None,
        min_liquidity: Optional[float] = None,
        max_staleness_seconds: Optional[float] = None,
    ) -> None:
        """Tune the gate's thresholds. Every kwarg is optional — callers
        that only want to override one threshold can leave the others at
        their defaults. Used by tests to set tight thresholds for the
        ``min_edge`` / ``max_spread`` / etc. rejection cases.
        """
        if min_edge is not None:
            self._min_edge = float(min_edge)
        if min_confidence is not None:
            self._min_confidence = float(min_confidence)
        if max_spread is not None:
            self._max_spread = float(max_spread)
        if min_liquidity is not None:
            self._min_liquidity = float(min_liquidity)
        if max_staleness_seconds is not None:
            self._max_staleness_seconds = float(max_staleness_seconds)

    # ── Public API ───────────────────────────────────────────────────────

    def check(
        self,
        order_request: dict,
        market_data: Optional[dict] = None,
        account_state: Optional[dict] = None,
    ) -> PreSubmissionResult:
        """Run ALL pre-submission checks.

        Args:
            order_request: ``{token_id, side, size, price, strategy,
                edge, confidence, order_id}``. The ``edge`` /
                ``confidence`` keys are optional — when absent, the
                corresponding checks are skipped (passed=True,
                message=_SKIPPED_MSG).
            market_data: ``{best_bid, best_ask, spread, liquidity,
                last_update}``. When ``None`` or ``{}``, the freshness /
                spread / liquidity checks are skipped (the gate cannot
                enforce microstructure checks without a book snapshot).
            account_state: ``{balance, total_exposure, open_orders,
                daily_pnl, drawdown, max_total_exposure,
                max_single_position, max_open_orders, daily_loss_limit,
                max_drawdown_limit}``. When ``None`` or ``{}``, the
                balance / exposure / single-position / open-orders /
                daily-loss / drawdown checks are skipped (the gate
                cannot enforce account-state checks without an account
                snapshot — the existing ``risk_manager.check_order``
                gate handles these via ``store`` when context is absent).

        Returns:
            ``PreSubmissionResult`` with ``approved=True`` only if every
            enforced check passed (skipped checks count as passed). When
            ``approved=False``, ``rejection_reason`` is the first failing
            check's ``message`` and ``rejection_category`` is its
            ``check_name``.
        """
        checks: list[RiskCheckResult] = []
        account_state = account_state or {}
        market_data = market_data or {}

        # 1. Kill switch — ALWAYS enforced (no input data required).
        checks.append(self._check_kill_switch())

        # 2-7. Account-state checks — skipped when account_state is empty.
        if account_state:
            checks.append(self._check_balance(order_request, account_state))
            checks.append(self._check_max_exposure(order_request, account_state))
            checks.append(self._check_max_single_position(order_request, account_state))
            checks.append(self._check_max_open_orders(account_state))
            checks.append(self._check_daily_loss_limit(account_state))
            checks.append(self._check_max_drawdown(account_state))
        else:
            for name in (
                "balance",
                "max_exposure",
                "max_single_position",
                "max_open_orders",
                "daily_loss_limit",
                "max_drawdown",
            ):
                checks.append(RiskCheckResult(
                    check_name=name,
                    passed=True,
                    value=None,
                    threshold=None,
                    message=_SKIPPED_MSG,
                ))

        # 8-10. Market-data checks — skipped when market_data is empty.
        if market_data:
            checks.append(self._check_data_freshness(market_data))
            checks.append(self._check_max_spread(market_data))
            checks.append(self._check_min_liquidity(market_data))
        else:
            for name in (
                "data_freshness",
                "max_spread",
                "min_liquidity",
            ):
                checks.append(RiskCheckResult(
                    check_name=name,
                    passed=True,
                    value=None,
                    threshold=None,
                    message=_SKIPPED_MSG,
                ))

        # 11. Edge — skipped when the order_request doesn't carry "edge".
        if "edge" in order_request:
            checks.append(self._check_min_edge(order_request))
        else:
            checks.append(RiskCheckResult(
                check_name="min_edge",
                passed=True,
                value=None,
                threshold=self._min_edge,
                message=_SKIPPED_MSG,
            ))

        # 12. Confidence — skipped when the order_request doesn't carry
        # "confidence".
        if "confidence" in order_request:
            checks.append(self._check_min_confidence(order_request))
        else:
            checks.append(RiskCheckResult(
                check_name="min_confidence",
                passed=True,
                value=None,
                threshold=self._min_confidence,
                message=_SKIPPED_MSG,
            ))

        # 13. Idempotency — ALWAYS enforced (uses deterministic key over
        # the 5-tuple; no caller-supplied context required).
        checks.append(self._check_idempotency(order_request))

        # 14. Circuit breaker — ALWAYS enforced (queries the CLOB breaker
        # singleton directly).
        checks.append(self._check_circuit_breaker())

        # Determine approval.
        failed = [c for c in checks if not c.passed]
        if failed:
            first_fail = failed[0]
            return PreSubmissionResult(
                approved=False,
                checks=checks,
                rejection_reason=first_fail.message,
                rejection_category=first_fail.check_name,
            )
        return PreSubmissionResult(
            approved=True,
            checks=checks,
        )

    # ── Individual checks ─────────────────────────────────────────────────

    def _check_kill_switch(self) -> RiskCheckResult:
        """Check #1 — durable kill switch not active."""
        try:
            from core.safety import kill_switch_file_exists
            ks_active = kill_switch_file_exists()
        except Exception as e:  # noqa: BLE001 — defensive: gate must never crash
            logger.error(
                "[pre_submission_gate] kill_switch check raised %r — "
                "FAIL CLOSED (treat as active)",
                e,
            )
            ks_active = True
        return RiskCheckResult(
            check_name="kill_switch",
            passed=not ks_active,
            value=ks_active,
            threshold=False,
            message=(
                "Kill switch is active" if ks_active else "OK"
            ),
        )

    def _check_balance(
        self, order_request: dict, account_state: dict,
    ) -> RiskCheckResult:
        """Check #2 — sufficient balance for the order cost."""
        balance = float(account_state.get("balance", 0) or 0)
        cost = float(order_request.get("size", 0) or 0) * float(
            order_request.get("price", 0) or 0
        )
        has_balance = balance >= cost
        return RiskCheckResult(
            check_name="balance",
            passed=has_balance,
            value=balance,
            threshold=cost,
            message=(
                f"Balance ${balance:.2f} < cost ${cost:.2f}"
                if not has_balance else "OK"
            ),
        )

    def _check_max_exposure(
        self, order_request: dict, account_state: dict,
    ) -> RiskCheckResult:
        """Check #3 — total exposure (existing + new cost) within cap."""
        total_exposure = float(account_state.get("total_exposure", 0) or 0)
        max_exposure = float(account_state.get("max_total_exposure", 25.0) or 25.0)
        cost = float(order_request.get("size", 0) or 0) * float(
            order_request.get("price", 0) or 0
        )
        new_exposure = total_exposure + cost
        exposure_ok = new_exposure <= max_exposure
        return RiskCheckResult(
            check_name="max_exposure",
            passed=exposure_ok,
            value=new_exposure,
            threshold=max_exposure,
            message=(
                f"Exposure ${new_exposure:.2f} > max ${max_exposure:.2f}"
                if not exposure_ok else "OK"
            ),
        )

    def _check_max_single_position(
        self, order_request: dict, account_state: dict,
    ) -> RiskCheckResult:
        """Check #4 — single-position cost within per-order cap."""
        max_single = float(account_state.get("max_single_position", 3.0) or 3.0)
        cost = float(order_request.get("size", 0) or 0) * float(
            order_request.get("price", 0) or 0
        )
        single_ok = cost <= max_single
        return RiskCheckResult(
            check_name="max_single_position",
            passed=single_ok,
            value=cost,
            threshold=max_single,
            message=(
                f"Single ${cost:.2f} > max ${max_single:.2f}"
                if not single_ok else "OK"
            ),
        )

    def _check_max_open_orders(self, account_state: dict) -> RiskCheckResult:
        """Check #5 — open-order count below max."""
        open_orders = int(account_state.get("open_orders", 0) or 0)
        max_orders = int(account_state.get("max_open_orders", 8) or 8)
        orders_ok = open_orders < max_orders
        return RiskCheckResult(
            check_name="max_open_orders",
            passed=orders_ok,
            value=open_orders,
            threshold=max_orders,
            message=(
                f"Open orders {open_orders} >= max {max_orders}"
                if not orders_ok else "OK"
            ),
        )

    def _check_daily_loss_limit(self, account_state: dict) -> RiskCheckResult:
        """Check #6 — daily P&L above the daily loss stop."""
        daily_pnl = float(account_state.get("daily_pnl", 0) or 0)
        daily_limit = float(account_state.get("daily_loss_limit", -2.0) or -2.0)
        # daily_limit is typically negative (e.g. -2.00 means "stop at -$2 loss").
        daily_ok = daily_pnl > daily_limit
        return RiskCheckResult(
            check_name="daily_loss_limit",
            passed=daily_ok,
            value=daily_pnl,
            threshold=daily_limit,
            message=(
                f"Daily P&L ${daily_pnl:.2f} below limit ${daily_limit:.2f}"
                if not daily_ok else "OK"
            ),
        )

    def _check_max_drawdown(self, account_state: dict) -> RiskCheckResult:
        """Check #7 — current drawdown below max."""
        drawdown = float(account_state.get("drawdown", 0) or 0)
        max_dd = float(account_state.get("max_drawdown_limit", 0.15) or 0.15)
        # drawdown expressed as a fraction (0.15 = 15%). Some callers pass
        # absolute dollars — we accept either as long as the caller is
        # consistent (drawdown AND max_drawdown_limit in the same unit).
        dd_ok = drawdown < max_dd
        return RiskCheckResult(
            check_name="max_drawdown",
            passed=dd_ok,
            value=drawdown,
            threshold=max_dd,
            message=(
                f"Drawdown {drawdown:.4f} > max {max_dd:.4f}"
                if not dd_ok else "OK"
            ),
        )

    def _check_data_freshness(self, market_data: dict) -> RiskCheckResult:
        """Check #8 — market data quote is fresh (last_update within
        the staleness window)."""
        last_update = float(market_data.get("last_update", 0) or 0)
        staleness = time.time() - last_update if last_update else 999.0
        fresh = staleness < self._max_staleness_seconds
        return RiskCheckResult(
            check_name="data_freshness",
            passed=fresh,
            value=f"{staleness:.1f}s",
            threshold=f"<{self._max_staleness_seconds}s",
            message=(
                f"Data is {staleness:.1f}s old (max {self._max_staleness_seconds}s)"
                if not fresh else "OK"
            ),
        )

    def _check_max_spread(self, market_data: dict) -> RiskCheckResult:
        """Check #9 — bid-ask spread within acceptable range."""
        spread = float(market_data.get("spread", 0) or 0)
        spread_ok = spread <= self._max_spread
        return RiskCheckResult(
            check_name="max_spread",
            passed=spread_ok,
            value=spread,
            threshold=self._max_spread,
            message=(
                f"Spread {spread:.4f} > max {self._max_spread}"
                if not spread_ok else "OK"
            ),
        )

    def _check_min_liquidity(self, market_data: dict) -> RiskCheckResult:
        """Check #10 — book liquidity meets the minimum notional."""
        liquidity = float(market_data.get("liquidity", 0) or 0)
        liq_ok = liquidity >= self._min_liquidity
        return RiskCheckResult(
            check_name="min_liquidity",
            passed=liq_ok,
            value=liquidity,
            threshold=self._min_liquidity,
            message=(
                f"Liquidity ${liquidity:.2f} < min ${self._min_liquidity}"
                if not liq_ok else "OK"
            ),
        )

    def _check_min_edge(self, order_request: dict) -> RiskCheckResult:
        """Check #11 — signal edge meets the minimum threshold."""
        edge = float(order_request.get("edge", 0) or 0)
        edge_ok = edge >= self._min_edge
        return RiskCheckResult(
            check_name="min_edge",
            passed=edge_ok,
            value=edge,
            threshold=self._min_edge,
            message=(
                f"Edge {edge:.4f} < min {self._min_edge}"
                if not edge_ok else "OK"
            ),
        )

    def _check_min_confidence(self, order_request: dict) -> RiskCheckResult:
        """Check #12 — signal confidence meets the minimum threshold."""
        confidence = float(order_request.get("confidence", 0) or 0)
        conf_ok = confidence >= self._min_confidence
        return RiskCheckResult(
            check_name="min_confidence",
            passed=conf_ok,
            value=confidence,
            threshold=self._min_confidence,
            message=(
                f"Confidence {confidence:.4f} < min {self._min_confidence}"
                if not conf_ok else "OK"
            ),
        )

    def _check_idempotency(self, order_request: dict) -> RiskCheckResult:
        """Check #13 — not a duplicate of a recent signal."""
        try:
            from core.idempotency import idempotency_manager
            idem_key = idempotency_manager.generate_key(
                strategy=order_request.get("strategy", ""),
                token_id=order_request.get("token_id", ""),
                side=order_request.get("side", ""),
                size=float(order_request.get("size", 0) or 0),
                price=float(order_request.get("price", 0) or 0),
            )
            is_dup, existing = idempotency_manager.check_and_record(
                idem_key,
                order_request.get("order_id", ""),
                order_request,
            )
        except Exception as e:  # noqa: BLE001 — defensive: gate must never crash
            logger.error(
                "[pre_submission_gate] idempotency check raised %r — "
                "PASS (cannot determine duplicate; allow order through)",
                e,
            )
            return RiskCheckResult(
                check_name="idempotency",
                passed=True,
                value="error",
                threshold=False,
                message=f"idempotency check errored ({e!r}) — passed",
            )
        return RiskCheckResult(
            check_name="idempotency",
            passed=not is_dup,
            value=is_dup,
            threshold=False,
            message=(
                f"Duplicate of order {existing}" if is_dup else "OK"
            ),
        )

    def _check_circuit_breaker(self) -> RiskCheckResult:
        """Check #14 — CLOB circuit breaker is CLOSED (or HALF_OPEN
        with capacity)."""
        try:
            from core.circuit_breaker import clob_breaker
            cb_ok = clob_breaker.can_execute()
            state_value = clob_breaker.state.value
        except Exception as e:  # noqa: BLE001 — defensive: gate must never crash
            logger.error(
                "[pre_submission_gate] circuit_breaker check raised %r — "
                "FAIL CLOSED (treat as open)",
                e,
            )
            return RiskCheckResult(
                check_name="circuit_breaker",
                passed=False,
                value="error",
                threshold="closed",
                message=f"circuit_breaker check errored ({e!r}) — blocked",
            )
        return RiskCheckResult(
            check_name="circuit_breaker",
            passed=cb_ok,
            value=state_value,
            threshold="closed",
            message=(
                f"CLOB circuit breaker is {state_value}"
                if not cb_ok else "OK"
            ),
        )


# Process-wide singleton — constructed at module-import time so every
# call site (BaseStrategy.submit_order, the API route, tests via
# monkeypatch) shares one gate with consistent thresholds.
pre_submission_gate = PreSubmissionGate()


__all__ = [
    "PreSubmissionGate",
    "PreSubmissionResult",
    "RiskCheckResult",
    "pre_submission_gate",
]
