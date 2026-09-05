"""Strategy lifecycle manager — enforces valid state transitions.

States::

    RESEARCH → EXPERIMENTAL → BACKTESTED → VALIDATED → PAPER →
    LIVE_CANDIDATE → LIVE → SUSPENDED → RETIRED

Rules:

- A profitable backtest alone must NEVER promote to LIVE.
- LIVE requires: out-of-sample validation, walk-forward, paper trading,
  risk checks, and explicit approval.
- SUSPENDED can be triggered by: the strategy health monitor, a manual
  operator action, or a risk breach.
- RETIRED is terminal — a retired strategy cannot be reactivated.
- All transitions are audited with timestamp, reason, and approver.

W37-2 — design contract.

The lifecycle manager is intentionally SYNC (no ``async``) so it can be
invoked from any context: a FastAPI handler (already inside an event
loop), a sync periodic sweep (the strategy health monitor), or a CLI /
REPL introspection. The audit trail is held in-memory (a per-strategy
list of ``LifecycleAuditEntry`` rows); a future task can durably
persist it via ``core.audit_logger`` if/when an operator asks for it.

The manager is decoupled from the existing ``StrategyRegistry`` /
``StrategyHealthMonitor`` so the two subsystems can evolve
independently:

- ``StrategyRegistry`` continues to own per-strategy instantiation +
  ``_disabled`` flag (W24-8 auto-disable).
- ``StrategyHealthMonitor`` continues to own the health snapshot +
  metric-driven auto-disable.
- ``StrategyLifecycleManager`` owns the **state-machine** that gates
  which transitions are allowed (a strategy that fails out-of-sample
  validation cannot be promoted to LIVE; a retired strategy cannot
  reactivate; etc.).

The integration point is ``register_strategy(strategy_name,
initial_state)`` — called by ``StrategyRegistry.start_strategy`` (or a
test fixture) the first time a strategy is seen so the lifecycle
manager knows the strategy exists. ``StrategyHealthMonitor._disable``
can then call ``transition(strategy_name, "SUSPENDED", reason="…",
approver="health-monitor")`` to record the suspension in the audit
trail alongside its existing ``StrategyRegistry.disable`` call.

The manager NEVER calls into ``StrategyRegistry.disable`` directly —
the W24-8 disable is the load-bearing safety primitive and we don't
want a second code path re-implementing it. The lifecycle manager's
job is the audit + state-machine gate; the W24-8 monitor owns the
actual stop.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# ── Lifecycle states ────────────────────────────────────────────────────────
# The state names match the task spec verbatim so API consumers (and the
# audit log) see the canonical W37-2 vocabulary regardless of how the
# existing ``STATUS_*`` constants in ``strategies/registry.py`` evolve.
# (``STATUS_*`` are about *implementation status* — IMPLEMENTED / PLANNED
# / EXPERIMENTAL — which is orthogonal to *lifecycle stage*. A strategy
# can be IMPLEMENTED but still in the RESEARCH lifecycle state.)

STATE_RESEARCH = "RESEARCH"
STATE_EXPERIMENTAL = "EXPERIMENTAL"
STATE_BACKTESTED = "BACKTESTED"
STATE_VALIDATED = "VALIDATED"
STATE_PAPER = "PAPER"
STATE_LIVE_CANDIDATE = "LIVE_CANDIDATE"
STATE_LIVE = "LIVE"
STATE_SUSPENDED = "SUSPENDED"
STATE_RETIRED = "RETIRED"

ALL_STATES: tuple[str, ...] = (
    STATE_RESEARCH,
    STATE_EXPERIMENTAL,
    STATE_BACKTESTED,
    STATE_VALIDATED,
    STATE_PAPER,
    STATE_LIVE_CANDIDATE,
    STATE_LIVE,
    STATE_SUSPENDED,
    STATE_RETIRED,
)

# ── Transition graph ──────────────────────────────────────────────────────────
# Keys are the *current* state; values are the set of states the strategy
# may transition *to*. A state not listed for the current state means the
# transition is forbidden (``transition`` raises ``InvalidTransitionError``).
#
# The graph encodes the W37-2 rules:
#
# - RESEARCH can only progress to EXPERIMENTAL (you can't skip the
#   experimental stage; the very first transition is to "we have a
#   hypothesis worth backtesting").
# - EXPERIMENTAL can go BACKTESTED (success) or RETIRED (give up).
# - BACKTESTED can revert to EXPERIMENTAL (the backtest failed and we
#   need to re-tune), progress to VALIDATED, or retire.
# - VALIDATED can revert to BACKTESTED, progress to PAPER, or retire.
# - PAPER can promote to LIVE_CANDIDATE, revert to VALIDATED, suspend,
#   or retire.
# - LIVE_CANDIDATE can promote to LIVE, revert to PAPER, or retire.
# - LIVE can only suspend or retire — never silently revert. (Suspending
#   is the explicit "pull it from production" action; retiring is
#   permanent.)
# - SUSPENDED can resume to PAPER (operator re-evaluation) or LIVE
#   (auto-resume after a transient breach cleared) or retire.
# - RETIRED is terminal — empty transition set.

VALID_TRANSITIONS: dict[str, list[str]] = {
    STATE_RESEARCH: [STATE_EXPERIMENTAL],
    STATE_EXPERIMENTAL: [STATE_BACKTESTED, STATE_RETIRED],
    STATE_BACKTESTED: [STATE_VALIDATED, STATE_EXPERIMENTAL, STATE_RETIRED],
    STATE_VALIDATED: [STATE_PAPER, STATE_BACKTESTED, STATE_RETIRED],
    STATE_PAPER: [
        STATE_LIVE_CANDIDATE,
        STATE_VALIDATED,
        STATE_SUSPENDED,
        STATE_RETIRED,
    ],
    STATE_LIVE_CANDIDATE: [STATE_LIVE, STATE_PAPER, STATE_RETIRED],
    STATE_LIVE: [STATE_SUSPENDED, STATE_RETIRED],
    STATE_SUSPENDED: [STATE_PAPER, STATE_LIVE, STATE_RETIRED],
    STATE_RETIRED: [],  # Terminal
}


# ── LIVE prerequisites ──────────────────────────────────────────────────────────
# Promoting to LIVE is the highest-stakes transition — a strategy that
# ships live capital must have cleared every independent validation gate
# in addition to its forward progression through the state graph.
# ``REQUIREMENTS_FOR_LIVE`` is the contract every ``transition(...,
# "LIVE", ...)`` call must satisfy; the caller supplies a ``requirements``
# dict on the transition call (the lifecycle manager doesn't query
# external systems itself — it's the caller's job to attest that the
# walk-forward + paper-trading + risk checks + approval have all cleared).

REQUIREMENTS_FOR_LIVE: dict[str, Any] = {
    "min_sample_size": 30,
    "min_out_of_sample_trades": 20,
    "min_sharpe": 0.5,
    "max_drawdown": 0.15,
    "requires_walk_forward": True,
    "requires_paper_validation": True,
    "requires_approval": True,
}

# The set of requirement keys the caller MUST explicitly attested in the
# ``requirements`` dict for a LIVE promotion to be accepted. Numeric
# thresholds are checked ``>=`` (or ``<=`` for max_drawdown) against the
# ``REQUIREMENTS_FOR_LIVE`` constant; booleans must be ``True``.
_LIVE_REQUIREMENT_KEYS: tuple[str, ...] = tuple(REQUIREMENTS_FOR_LIVE.keys())


class InvalidTransitionError(ValueError):
    """Raised when a lifecycle transition is rejected.

    Carries ``from_state`` / ``to_state`` / ``reason`` on the instance so
    API error handlers can surface a structured 400 response (vs. an
    opaque ValueError message). The ``reason`` attribute is the
    human-readable rejection explanation ("RETIRED is terminal",
    "live promotion missing requirements: …", etc.) — distinct from the
    ``reason`` argument the caller passes on the transition itself
    (which is the operator's free-form justification for the transition).
    """

    def __init__(
        self,
        from_state: str,
        to_state: str,
        reason: str,
    ) -> None:
        super().__init__(
            f"Invalid lifecycle transition {from_state!r} → {to_state!r}: {reason}"
        )
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason


@dataclass
class LifecycleAuditEntry:
    """One row in a strategy's lifecycle audit trail.

    A new entry is appended on every successful transition (the
    ``from_state`` is the state BEFORE the transition; ``to_state`` is
    the state AFTER). The ``approver`` is the identity that authorised
    the transition — typically ``"operator"`` for manual API calls,
    ``"health-monitor"`` for W24-8 auto-suspensions, or ``"risk-engine"``
    for risk-breach-driven suspensions.

    ``metadata`` is an optional plain dict for strategy-specific context
    (e.g. ``{"sharpe": 0.82, "max_drawdown": 0.12, "oos_trades": 25}``
    on a LIVE promotion so the audit row is self-contained — an
    operator can read the row and see exactly what evidence was used
    to justify the promotion, not just that "some requirements were
    met").
    """

    timestamp: float
    from_state: str
    to_state: str
    reason: str
    approver: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view of this audit row.

        ``asdict`` would suffice for the standard fields but ``metadata``
        is already a plain dict (no nested dataclasses) so we just
        delegate. The helper exists so the API route can call
        ``.to_dict()`` uniformly instead of reaching into ``__dict__``
        (the convention in this repo — see ``StrategyHealth.to_dict``).
        """
        return asdict(self)


class StrategyLifecycleManager:
    """Per-strategy lifecycle state machine + audit trail.

    The manager holds:

    - ``_states``: ``{strategy_name: current_state}`` — the latest
      known lifecycle state of each registered strategy. Defaults to
      ``RESEARCH`` on first ``register_strategy`` call.
    - ``_history``: ``{strategy_name: list[LifecycleAuditEntry]}`` — the
      full ordered audit trail of every transition that has succeeded.

    Both dicts are keyed by strategy_name so the same manager instance
    tracks every strategy in the bot — mirrors the
    ``StrategyHealthMonitor._health`` pattern.
    """

    def __init__(self) -> None:
        self._states: dict[str, str] = {}
        self._history: dict[str, list[LifecycleAuditEntry]] = {}

    # ── Registration ────────────────────────────────────────────────────

    def register_strategy(
        self,
        strategy_name: str,
        initial_state: str = STATE_RESEARCH,
    ) -> str:
        """Idempotently register a strategy with the lifecycle manager.

        If the strategy is already registered, the existing state is
        returned unchanged (this method never throws for a duplicate
        registration — it's the caller's contract that registration is
        best-effort). If the strategy is new, an initial RESEARCH state
        is recorded (or ``initial_state`` if the caller supplies one —
        used by tests that want to seed a strategy mid-pipeline).

        Returns the current state (post-registration).
        """
        if strategy_name in self._states:
            return self._states[strategy_name]
        if initial_state not in VALID_TRANSITIONS:
            raise ValueError(
                f"Unknown initial lifecycle state: {initial_state!r}. "
                f"Valid states: {list(VALID_TRANSITIONS.keys())}"
            )
        self._states[strategy_name] = initial_state
        self._history[strategy_name] = []
        log.info(
            "[lifecycle] Registered %s at state %s",
            strategy_name, initial_state,
        )
        return initial_state

    # ── Read APIs ────────────────────────────────────────────────────────

    def get_state(self, strategy_name: str) -> str | None:
        """Return the current lifecycle state, or ``None`` if the
        strategy hasn't been registered yet.

        ``None`` (vs. raising) is the contract because callers like
        ``StrategyHealthMonitor._disable`` may want to record a
        suspension for a strategy that the lifecycle manager hasn't
        seen yet — they should call ``register_strategy`` first, then
        ``transition``. Returning ``None`` lets the caller decide whether
        to auto-register or to skip the audit (a strategy the operator
        hasn't explicitly promoted should NOT silently get a LIVE
        lifecycle row just because the health monitor fired).
        """
        return self._states.get(strategy_name)

    def get_history(self, strategy_name: str) -> list[dict[str, Any]]:
        """Return the audit trail as a list of plain dicts.

        Returns ``[]`` for an unknown strategy (vs. raising) so the
        ``GET /api/strategies/{name}/lifecycle`` route can return a 404
        with a clean message instead of a 500 from an unhandled
        KeyError. The caller (the route handler) decides whether an
        empty trail is a 404 (unknown strategy) or a 200 with an empty
        list (registered strategy with no transitions yet).
        """
        entries = self._history.get(strategy_name, [])
        return [e.to_dict() for e in entries]

    def is_terminal(self, strategy_name: str) -> bool:
        """Return True iff the strategy is in the RETIRED state.

        Convenience wrapper around ``get_state(name) == STATE_RETIRED``
        so callers don't need to import the ``STATE_RETIRED`` constant
        directly. Used by the API route's 409 response (a transition
        attempt on a retired strategy).
        """
        return self._states.get(strategy_name) == STATE_RETIRED

    # ── Pure validation (no state mutation) ──────────────────────────────

    @staticmethod
    def validate_transition(
        from_state: str,
        to_state: str,
        requirements: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """Pure validation — returns ``(ok, reason)``.

        ``ok`` is True iff:

        1. ``from_state`` is a known state.
        2. ``to_state`` is a known state.
        3. ``to_state`` is in ``VALID_TRANSITIONS[from_state]``.
        4. If ``to_state == LIVE``, every key in
           ``REQUIREMENTS_FOR_LIVE`` is present in ``requirements`` and
           each value meets its threshold (numeric ``>=`` for
           ``min_*``, numeric ``<=`` for ``max_*``, truthy for
           ``requires_*`` booleans).

        ``reason`` is ``""`` when ``ok`` is True, or a human-readable
        rejection explanation when False. The method does NOT raise —
        it's the caller's choice whether to raise
        ``InvalidTransitionError`` (the ``transition`` method does) or
        to surface the reason to the user (the API route may).

        This is a ``@staticmethod`` so tests / external callers can
        validate a hypothetical transition without registering a
        strategy first.
        """
        # ── (1) + (2) Known states. ─────────────────────────────────────
        if from_state not in VALID_TRANSITIONS:
            return False, f"Unknown from_state: {from_state!r}"
        if to_state not in VALID_TRANSITIONS:
            return False, f"Unknown to_state: {to_state!r}"

        # ── (3) Edge exists in the graph. ──────────────────────────────
        if to_state not in VALID_TRANSITIONS[from_state]:
            allowed = VALID_TRANSITIONS[from_state]
            return False, (
                f"{from_state!r} → {to_state!r} not in allowed "
                f"transitions {allowed}"
            )

        # ── (4) LIVE requires all prerequisites met. ─────────────────
        # A profitable backtest alone must NEVER promote to LIVE — the
        # caller must attests to out-of-sample validation, walk-forward,
        # paper trading, risk checks, and explicit approval. Every key
        # in REQUIREMENTS_FOR_LIVE must be present in the supplied
        # ``requirements`` dict AND the value must clear its threshold.
        if to_state == STATE_LIVE:
            reqs = requirements or {}
            missing: list[str] = []
            failed: list[str] = []
            for key in _LIVE_REQUIREMENT_KEYS:
                if key not in reqs:
                    missing.append(key)
                    continue
                value = reqs[key]
                threshold = REQUIREMENTS_FOR_LIVE[key]
                if isinstance(threshold, bool):
                    if not value:
                        failed.append(f"{key}=False (must be True)")
                elif key.startswith("min_"):
                    # Numeric minimum — value must be >= threshold.
                    if not isinstance(value, (int, float)) or value < threshold:
                        failed.append(
                            f"{key}={value} (< min {threshold})"
                        )
                elif key.startswith("max_"):
                    # Numeric maximum — value must be <= threshold.
                    if not isinstance(value, (int, float)) or value > threshold:
                        failed.append(
                            f"{key}={value} (> max {threshold})"
                        )
            if missing:
                return False, (
                    f"LIVE promotion missing requirements: "
                    f"{', '.join(missing)}"
                )
            if failed:
                return False, (
                    f"LIVE promotion requirements failed: "
                    f"{'; '.join(failed)}"
                )

        return True, ""

    # ── State mutation ──────────────────────────────────────────────────

    def transition(
        self,
        strategy_name: str,
        target_state: str,
        reason: str = "",
        approver: str = "operator",
        requirements: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate + apply a lifecycle transition.

        On success: appends a ``LifecycleAuditEntry`` to the strategy's
        audit trail, updates ``_states[strategy_name]``, and returns the
        new state as ``{"strategy": name, "state": target_state,
        "audit": entry.to_dict()}`` so the API route can return it
        directly.

        On failure: raises ``InvalidTransitionError`` (with the
        structured ``from_state`` / ``to_state`` / ``reason`` attributes)
        so the API route can convert it to a 400/409 response.

        ``reason`` is the operator's free-form justification ("manual
        promotion after paper-trading review"). ``approver`` is the
        identity authorising the transition — defaults to ``"operator"``
        for manual API calls; the strategy health monitor passes
        ``"health-monitor"``; the risk engine passes ``"risk-engine"``.
        ``requirements`` is the LIVE-prerequisite attestation dict
        (only consulted when ``target_state == "LIVE"``); see
        ``validate_transition`` for the threshold semantics.
        """
        from_state = self._states.get(strategy_name)
        if from_state is None:
            # Unknown strategy — auto-register at RESEARCH so the audit
            # trail starts cleanly. (A transition call on an unregistered
            # strategy is treated as "register + attempt transition";
            # if the transition itself is invalid the registration
            # remains but no audit row is appended.)
            from_state = self.register_strategy(strategy_name)

        ok, why = self.validate_transition(
            from_state, target_state, requirements=requirements,
        )
        if not ok:
            raise InvalidTransitionError(from_state, target_state, why)

        # ── Apply the transition. ─────────────────────────────────────
        entry = LifecycleAuditEntry(
            timestamp=time.time(),
            from_state=from_state,
            to_state=target_state,
            reason=reason,
            approver=approver,
            metadata=dict(requirements) if target_state == STATE_LIVE else {},
        )
        self._states[strategy_name] = target_state
        self._history.setdefault(strategy_name, []).append(entry)
        log.info(
            "[lifecycle] %s: %s → %s (approver=%s, reason=%s)",
            strategy_name, from_state, target_state, approver,
            reason or "(none)",
        )
        return {
            "strategy": strategy_name,
            "state": target_state,
            "audit": entry.to_dict(),
        }


# ── Module-level singleton (mirrors the pattern in every other core/
# strategies module — ``strategy_registry``, ``strategy_health_monitor``,
# ``paper_sim``, etc.). The API routes and the strategy health monitor
# both reference this singleton so the in-memory state is shared.
strategy_lifecycle = StrategyLifecycleManager()
