"""
Unit + integration tests for the W37-2 strategy lifecycle manager.

Covers:

  (1) ``StrategyLifecycleManager.validate_transition`` — the pure
      validation function:
        * valid forward transition (RESEARCH → EXPERIMENTAL) → ok
        * invalid forward transition (RESEARCH → LIVE) → rejected
        * graph violation (PAPER → RESEARCH) → rejected
        * unknown from_state / to_state → rejected
        * LIVE requires ALL prerequisites present
        * LIVE rejects missing requirement keys
        * LIVE rejects threshold violations (sharpe too low, drawdown too high)
        * LIVE rejects boolean ``requires_*`` flags set to False
        * RETIRED → anything is rejected (terminal)
  (2) ``StrategyLifecycleManager.transition`` — the state-mutating API:
        * happy-path progression through the full lifecycle
          (RESEARCH → EXPERIMENTAL → BACKTESTED → VALIDATED →
          PAPER → LIVE_CANDIDATE → LIVE → SUSPENDED → RETIRED)
        * transition appends an audit row with correct fields
        * invalid transition raises ``InvalidTransitionError``
        * RETIRED is terminal — subsequent transitions rejected
        * audit trail is ordered chronologically (oldest first)
        * approver + reason + metadata recorded on the audit row
  (3) ``register_strategy`` idempotency — registering the same strategy
      twice is a no-op; registering with an unknown initial state raises.
  (4) ``get_state`` / ``get_history`` — read APIs return None / [] for
      unknown strategies.
  (5) LIVE-prerequisite contract — the ``REQUIREMENTS_FOR_LIVE`` dict
      carries the documented thresholds and the validation function
      enforces each one.
  (6) API routes via ``TestClient``:
        POST /api/strategies/{name}/transition  → 200 happy-path
        POST /api/strategies/{name}/transition  → 400 invalid transition
        POST /api/strategies/{name}/transition  → 400 missing LIVE reqs
        POST /api/strategies/{name}/transition  → 409 RETIRED terminal
        GET  /api/strategies/{name}/lifecycle  → 200 with audit trail
        GET  /api/strategies/{name}/lifecycle  → 404 unknown strategy
        GET  /api/strategies/{name}/lifecycle  → 401 without auth

Each test constructs a fresh ``StrategyLifecycleManager`` so there is
zero state leakage between tests. The module-level singleton
``strategy_lifecycle`` is monkeypatched in the API-route tests so the
route handlers (which reference it directly via closure) hit an isolated
manager instance — mirrors the pattern in
``tests/test_strategy_health.py``.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Make the polymarket-bot package root importable as top-level modules
# (``strategies.lifecycle``) regardless of the cwd pytest was launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (sys.path must be set first)

from fastapi.testclient import TestClient  # noqa: E402

from strategies.lifecycle import (  # noqa: E402
    ALL_STATES,
    InvalidTransitionError,
    REQUIREMENTS_FOR_LIVE,
    STATE_BACKTESTED,
    STATE_EXPERIMENTAL,
    STATE_LIVE,
    STATE_LIVE_CANDIDATE,
    STATE_PAPER,
    STATE_RESEARCH,
    STATE_RETIRED,
    STATE_SUSPENDED,
    STATE_VALIDATED,
    VALID_TRANSITIONS,
    LifecycleAuditEntry,
    StrategyLifecycleManager,
    strategy_lifecycle,
)

# All tests in this module are SYNC ``def`` (not ``async def``) so they
# run cleanly under ``TestClient``'s sync portal — mirrors the
# convention in ``tests/test_strategy_health.py`` /
# ``tests/test_alerting.py``. No ``pytestmark = pytest.mark.asyncio``
# is needed.

VALID_TOKEN = "test-token-conftest"  # set by conftest.py

# A stable test strategy_id from the real catalog so the API-route
# tests exercise the actual server path (not a fabricated id that the
# catalog lookup would reject).
_TEST_STRATEGY_ID = "mm_avellaneda_stoikov"

# A complete LIVE-prerequisite attestation dict that clears every
# threshold in ``REQUIREMENTS_FOR_LIVE``. Tests that want to exercise a
# missing / failing requirement copy this dict and remove / mutate one
# key so the rest of the attestation is valid (isolates the one branch
# under test, mirroring the strategy-health monitor test pattern).
_FULL_LIVE_REQUIREMENTS = {
    "min_sample_size": 30,
    "min_out_of_sample_trades": 20,
    "min_sharpe": 0.5,
    "max_drawdown": 0.15,
    "requires_walk_forward": True,
    "requires_paper_validation": True,
    "requires_approval": True,
}


# ── Fixture: fresh manager per test ─────────────────────────────────────────
@pytest.fixture
def manager():
    """Fresh ``StrategyLifecycleManager`` per test (no shared ``_states``
    or ``_history``).

    The module-level singleton ``strategy_lifecycle`` is left untouched —
    the API-route tests below monkeypatch it explicitly.
    """
    return StrategyLifecycleManager()


# ── Helper: walk a strategy through the full lifecycle in one call ─────────
def _walk_to_live(
    mgr: StrategyLifecycleManager,
    name: str = "test_strat",
    requirements: dict | None = None,
) -> str:
    """Walk a strategy from RESEARCH → LIVE via the happy path.

    Returns the final state ("LIVE" on success). Used by tests that
    want to verify the LIVE state itself (audit trail shape, terminal
    RETIRED transition, etc.) without re-stating every intermediate
    transition in every test.
    """
    mgr.register_strategy(name)
    mgr.transition(name, STATE_EXPERIMENTAL, reason="hypothesis formed")
    mgr.transition(name, STATE_BACKTESTED, reason="backtest profitable")
    mgr.transition(name, STATE_VALIDATED, reason="oos validation passed")
    mgr.transition(name, STATE_PAPER, reason="paper trading starts")
    mgr.transition(
        name, STATE_LIVE_CANDIDATE, reason="paper trading successful",
    )
    mgr.transition(
        name, STATE_LIVE,
        reason="promoted to live after approval",
        approver="operator",
        requirements=requirements or _FULL_LIVE_REQUIREMENTS,
    )
    return mgr.get_state(name)


# ── (1) validate_transition — pure validation ────────────────────────────────

def test_validate_transition_happy_path_forward():
    """RESEARCH → EXPERIMENTAL is in ``VALID_TRANSITIONS`` and so
    ``validate_transition`` returns ``(True, "")``."""
    ok, reason = StrategyLifecycleManager.validate_transition(
        STATE_RESEARCH, STATE_EXPERIMENTAL,
    )
    assert ok is True
    assert reason == ""


def test_validate_transition_rejects_graph_violation():
    """RESEARCH → LIVE is not in ``VALID_TRANSITIONS[RESEARCH]`` (only
    EXPERIMENTAL is) so the transition is rejected with a
    graph-violation reason."""
    ok, reason = StrategyLifecycleManager.validate_transition(
        STATE_RESEARCH, STATE_LIVE,
    )
    assert ok is False
    assert "not in allowed transitions" in reason
    assert STATE_EXPERIMENTAL in reason  # reason names the allowed set


def test_validate_transition_rejects_unknown_from_state():
    """An unknown ``from_state`` (not in ``VALID_TRANSITIONS``) is
    rejected with an "Unknown from_state" reason."""
    ok, reason = StrategyLifecycleManager.validate_transition(
        "BOGUS", STATE_EXPERIMENTAL,
    )
    assert ok is False
    assert "Unknown from_state" in reason
    assert "BOGUS" in reason


def test_validate_transition_rejects_unknown_to_state():
    """An unknown ``to_state`` is rejected with an "Unknown to_state"
    reason (mirrors the from_state check)."""
    ok, reason = StrategyLifecycleManager.validate_transition(
        STATE_RESEARCH, "BOGUS",
    )
    assert ok is False
    assert "Unknown to_state" in reason
    assert "BOGUS" in reason


def test_validate_transition_research_to_paper_blocked():
    """PAPER is reachable from VALIDATED but NOT from RESEARCH — the
    caller cannot skip the experimental + backtested + validated
    stages."""
    ok, reason = StrategyLifecycleManager.validate_transition(
        STATE_RESEARCH, STATE_PAPER,
    )
    assert ok is False
    assert "not in allowed transitions" in reason


def test_validate_transition_paper_to_research_blocked():
    """Reverting PAPER → RESEARCH is forbidden (the graph has no such
    edge — once a strategy is paper-trading, the only forward path is
    LIVE_CANDIDATE; the backward path is VALIDATED, not RESEARCH)."""
    ok, reason = StrategyLifecycleManager.validate_transition(
        STATE_PAPER, STATE_RESEARCH,
    )
    assert ok is False
    assert "not in allowed transitions" in reason


def test_validate_transition_retired_to_anything_blocked():
    """RETIRED is terminal — ``VALID_TRANSITIONS[RETIRED] == []`` so
    no transition out of RETIRED is ever valid."""
    for target in ALL_STATES:
        if target == STATE_RETIRED:
            continue  # self-transition also forbidden (not in [])
        ok, _ = StrategyLifecycleManager.validate_transition(
            STATE_RETIRED, target,
        )
        assert ok is False, (
            f"RETIRED → {target} must be rejected (terminal state)"
        )


def test_validate_transition_live_requires_all_requirements():
    """LIVE promotion requires EVERY key in
    ``REQUIREMENTS_FOR_LIVE`` present in the supplied ``requirements``
    dict. Missing any one of them is a rejection."""
    # Drop one key at a time and assert rejection.
    for missing_key in REQUIREMENTS_FOR_LIVE:
        reqs = dict(_FULL_LIVE_REQUIREMENTS)
        del reqs[missing_key]
        ok, reason = StrategyLifecycleManager.validate_transition(
            STATE_LIVE_CANDIDATE, STATE_LIVE, requirements=reqs,
        )
        assert ok is False, (
            f"LIVE promotion must reject missing '{missing_key}'"
        )
        assert "missing requirements" in reason
        assert missing_key in reason


def test_validate_transition_live_accepts_full_requirements():
    """A complete attestation (every key present, every threshold met)
    is accepted from LIVE_CANDIDATE → LIVE."""
    ok, reason = StrategyLifecycleManager.validate_transition(
        STATE_LIVE_CANDIDATE, STATE_LIVE,
        requirements=_FULL_LIVE_REQUIREMENTS,
    )
    assert ok is True
    assert reason == ""


def test_validate_transition_live_rejects_low_sharpe():
    """``min_sharpe`` is a numeric minimum — a value below the
    threshold (0.4 < 0.5) is rejected even when every other requirement
    is satisfied."""
    reqs = dict(_FULL_LIVE_REQUIREMENTS)
    reqs["min_sharpe"] = 0.4
    ok, reason = StrategyLifecycleManager.validate_transition(
        STATE_LIVE_CANDIDATE, STATE_LIVE, requirements=reqs,
    )
    assert ok is False
    assert "min_sharpe" in reason
    assert "< min" in reason


def test_validate_transition_live_rejects_high_drawdown():
    """``max_drawdown`` is a numeric maximum — a value above the
    threshold (0.20 > 0.15) is rejected."""
    reqs = dict(_FULL_LIVE_REQUIREMENTS)
    reqs["max_drawdown"] = 0.20
    ok, reason = StrategyLifecycleManager.validate_transition(
        STATE_LIVE_CANDIDATE, STATE_LIVE, requirements=reqs,
    )
    assert ok is False
    assert "max_drawdown" in reason
    assert "> max" in reason


def test_validate_transition_live_rejects_low_sample_size():
    """``min_sample_size`` is a numeric minimum — fewer than 30
    samples is rejected."""
    reqs = dict(_FULL_LIVE_REQUIREMENTS)
    reqs["min_sample_size"] = 29
    ok, reason = StrategyLifecycleManager.validate_transition(
        STATE_LIVE_CANDIDATE, STATE_LIVE, requirements=reqs,
    )
    assert ok is False
    assert "min_sample_size" in reason


def test_validate_transition_live_rejects_low_oos_trades():
    """``min_out_of_sample_trades`` is a numeric minimum — fewer than
    20 OOS trades is rejected (a profitable backtest alone must NEVER
    promote to LIVE)."""
    reqs = dict(_FULL_LIVE_REQUIREMENTS)
    reqs["min_out_of_sample_trades"] = 19
    ok, reason = StrategyLifecycleManager.validate_transition(
        STATE_LIVE_CANDIDATE, STATE_LIVE, requirements=reqs,
    )
    assert ok is False
    assert "min_out_of_sample_trades" in reason


def test_validate_transition_live_rejects_false_walk_forward():
    """``requires_walk_forward`` is a boolean — must be True. ``False``
    is rejected even when every numeric threshold is met."""
    reqs = dict(_FULL_LIVE_REQUIREMENTS)
    reqs["requires_walk_forward"] = False
    ok, reason = StrategyLifecycleManager.validate_transition(
        STATE_LIVE_CANDIDATE, STATE_LIVE, requirements=reqs,
    )
    assert ok is False
    assert "requires_walk_forward" in reason
    assert "False" in reason


def test_validate_transition_live_rejects_false_paper_validation():
    """``requires_paper_validation=False`` is rejected — LIVE requires
    paper trading to have cleared."""
    reqs = dict(_FULL_LIVE_REQUIREMENTS)
    reqs["requires_paper_validation"] = False
    ok, reason = StrategyLifecycleManager.validate_transition(
        STATE_LIVE_CANDIDATE, STATE_LIVE, requirements=reqs,
    )
    assert ok is False
    assert "requires_paper_validation" in reason


def test_validate_transition_live_rejects_false_approval():
    """``requires_approval=False`` is rejected — LIVE requires explicit
    operator approval."""
    reqs = dict(_FULL_LIVE_REQUIREMENTS)
    reqs["requires_approval"] = False
    ok, reason = StrategyLifecycleManager.validate_transition(
        STATE_LIVE_CANDIDATE, STATE_LIVE, requirements=reqs,
    )
    assert ok is False
    assert "requires_approval" in reason


def test_validate_transition_live_rejects_no_requirements_dict():
    """A LIVE promotion with no ``requirements`` dict at all is
    rejected — every key is missing."""
    ok, reason = StrategyLifecycleManager.validate_transition(
        STATE_LIVE_CANDIDATE, STATE_LIVE, requirements=None,
    )
    assert ok is False
    assert "missing requirements" in reason


def test_validate_transition_live_from_non_candidate_rejected_by_graph():
    """A LIVE promotion from BACKTESTED is rejected by the GRAPH
    (BACKTESTED's allowed set is [VALIDATED, EXPERIMENTAL, RETIRED]),
    NOT by the LIVE requirements check — the graph check fires first
    so a strategy can't skip LIVE_CANDIDATE."""
    ok, reason = StrategyLifecycleManager.validate_transition(
        STATE_BACKTESTED, STATE_LIVE,
        requirements=_FULL_LIVE_REQUIREMENTS,
    )
    assert ok is False
    # The reason should be a graph violation, not a missing-requirements
    # violation (the LIVE requirements check is gated behind the graph
    # check — only runs if the edge exists).
    assert "not in allowed transitions" in reason
    assert "missing requirements" not in reason


# ── (2) transition — state mutation + audit trail ─────────────────────────────

def test_transition_appends_audit_entry(manager):
    """A successful transition appends a ``LifecycleAuditEntry`` to the
    strategy's history with the correct ``from_state`` / ``to_state`` /
    ``reason`` / ``approver``."""
    manager.register_strategy("strat_a")
    before = len(manager.get_history("strat_a"))
    result = manager.transition(
        "strat_a", STATE_EXPERIMENTAL,
        reason="hypothesis formed", approver="operator",
    )
    after = len(manager.get_history("strat_a"))

    assert after == before + 1
    audit = result["audit"]
    assert audit["from_state"] == STATE_RESEARCH
    assert audit["to_state"] == STATE_EXPERIMENTAL
    assert audit["reason"] == "hypothesis formed"
    assert audit["approver"] == "operator"
    assert audit["timestamp"] > 0
    assert result["strategy"] == "strat_a"
    assert result["state"] == STATE_EXPERIMENTAL


def test_transition_invalid_raises(manager):
    """An invalid transition raises ``InvalidTransitionError`` with the
    structured ``from_state`` / ``to_state`` / ``reason`` attributes."""
    manager.register_strategy("strat_b")
    with pytest.raises(InvalidTransitionError) as exc_info:
        manager.transition("strat_b", STATE_LIVE)
    err = exc_info.value
    assert err.from_state == STATE_RESEARCH
    assert err.to_state == STATE_LIVE
    assert "not in allowed transitions" in err.reason


def test_transition_live_missing_requirements_raises(manager):
    """A LIVE promotion with missing requirements raises
    ``InvalidTransitionError`` whose ``reason`` mentions the missing
    key."""
    manager.register_strategy("strat_c")
    # Walk to LIVE_CANDIDATE first.
    manager.transition("strat_c", STATE_EXPERIMENTAL)
    manager.transition("strat_c", STATE_BACKTESTED)
    manager.transition("strat_c", STATE_VALIDATED)
    manager.transition("strat_c", STATE_PAPER)
    manager.transition("strat_c", STATE_LIVE_CANDIDATE)

    reqs = dict(_FULL_LIVE_REQUIREMENTS)
    del reqs["requires_approval"]
    with pytest.raises(InvalidTransitionError) as exc_info:
        manager.transition("strat_c", STATE_LIVE, requirements=reqs)
    assert "requires_approval" in exc_info.value.reason
    # State must NOT have changed.
    assert manager.get_state("strat_c") == STATE_LIVE_CANDIDATE


def test_transition_retired_is_terminal(manager):
    """Once a strategy is RETIRED, every subsequent transition raises
    ``InvalidTransitionError`` — RETIRED cannot reactivate."""
    manager.register_strategy("strat_d")
    # Walk to RETIRED via the shortest path (RESEARCH → EXPERIMENTAL →
    # RETIRED — VALID_TRANSITIONS[EXPERIMENTAL] includes RETIRED).
    manager.transition("strat_d", STATE_EXPERIMENTAL)
    manager.transition("strat_d", STATE_RETIRED, reason="abandoned")
    assert manager.get_state("strat_d") == STATE_RETIRED

    # Every target state should now be rejected.
    for target in ALL_STATES:
        if target == STATE_RETIRED:
            continue
        with pytest.raises(InvalidTransitionError) as exc_info:
            manager.transition("strat_d", target)
        assert exc_info.value.from_state == STATE_RETIRED


def test_transition_walks_full_happy_path(manager):
    """A strategy can walk the entire documented lifecycle in order:
    RESEARCH → EXPERIMENTAL → BACKTESTED → VALIDATED → PAPER →
    LIVE_CANDIDATE → LIVE → SUSPENDED → RETIRED. Each transition
    succeeds; the final state is RETIRED."""
    state = _walk_to_live(manager, "happy_strat")
    assert state == STATE_LIVE

    # Now suspend + retire.
    manager.transition("happy_strat", STATE_SUSPENDED, reason="risk breach")
    assert manager.get_state("happy_strat") == STATE_SUSPENDED
    manager.transition("happy_strat", STATE_LIVE, reason="risk cleared",
                        requirements=_FULL_LIVE_REQUIREMENTS)
    assert manager.get_state("happy_strat") == STATE_LIVE
    manager.transition("happy_strat", STATE_RETIRED, reason="end of life")
    assert manager.get_state("happy_strat") == STATE_RETIRED


def test_transition_audit_trail_is_ordered_chronologically(manager):
    """The audit trail is appended in transition order — the FIRST
    entry is the RESEARCH → EXPERIMENTAL transition, the LAST entry is
    the most recent transition. Timestamps are monotonically
    non-decreasing."""
    manager.register_strategy("audit_strat")
    # Insert two transitions with a measurable time gap so the
    # timestamp ordering is observable (not just insertion order).
    manager.transition("audit_strat", STATE_EXPERIMENTAL, reason="first")
    time.sleep(0.005)
    manager.transition("audit_strat", STATE_BACKTESTED, reason="second")
    time.sleep(0.005)
    manager.transition("audit_strat", STATE_VALIDATED, reason="third")

    history = manager.get_history("audit_strat")
    assert len(history) == 3
    assert history[0]["from_state"] == STATE_RESEARCH
    assert history[0]["to_state"] == STATE_EXPERIMENTAL
    assert history[0]["reason"] == "first"
    assert history[-1]["to_state"] == STATE_VALIDATED
    # Timestamps must be non-decreasing.
    ts = [h["timestamp"] for h in history]
    assert ts == sorted(ts), (
        f"audit timestamps must be non-decreasing; got {ts}"
    )
    # Strictly increasing (we slept between transitions).
    assert ts[1] > ts[0]
    assert ts[2] > ts[1]


def test_transition_records_approver_and_reason(manager):
    """The audit row carries the ``approver`` and ``reason`` exactly as
    supplied by the caller — distinct values for distinct transitions
    are NOT collapsed."""
    manager.register_strategy("appr_strat")
    manager.transition(
        "appr_strat", STATE_EXPERIMENTAL,
        reason="manual promotion", approver="operator",
    )
    manager.transition(
        "appr_strat", STATE_BACKTESTED,
        reason="backtest cleared", approver="backtest-engine",
    )
    history = manager.get_history("appr_strat")
    assert history[0]["approver"] == "operator"
    assert history[0]["reason"] == "manual promotion"
    assert history[1]["approver"] == "backtest-engine"
    assert history[1]["reason"] == "backtest cleared"


def test_transition_live_records_requirements_in_metadata(manager):
    """A successful LIVE promotion records the supplied
    ``requirements`` dict in the audit row's ``metadata`` field so the
    audit trail is self-contained — an operator reading the audit can
    see exactly which thresholds were attested."""
    _walk_to_live(manager, "live_meta_strat")
    history = manager.get_history("live_meta_strat")
    live_entry = history[-1]
    assert live_entry["to_state"] == STATE_LIVE
    assert live_entry["metadata"] == _FULL_LIVE_REQUIREMENTS


def test_transition_non_live_does_not_record_metadata(manager):
    """Non-LIVE transitions do NOT record a ``metadata`` field (the
    dict is empty) — the LIVE requirements dict is the only metadata
    surfaced today."""
    manager.register_strategy("meta_strat")
    manager.transition("meta_strat", STATE_EXPERIMENTAL)
    history = manager.get_history("meta_strat")
    assert history[-1]["metadata"] == {}


def test_transition_auto_registers_unknown_strategy(manager):
    """A ``transition`` call on an unregistered strategy auto-registers
    it (at RESEARCH) and then attempts the transition. If the
    transition itself is invalid, the strategy remains registered at
    RESEARCH (no audit row appended); if valid, the audit row is
    appended as usual."""
    # Unknown strategy + valid first transition (RESEARCH → EXPERIMENTAL)
    # → audit row appended + state is EXPERIMENTAL.
    manager.transition("auto_reg", STATE_EXPERIMENTAL, reason="first")
    assert manager.get_state("auto_reg") == STATE_EXPERIMENTAL
    assert len(manager.get_history("auto_reg")) == 1

    # Unknown strategy + invalid first transition (RESEARCH → LIVE)
    # → no audit row appended + state stays at RESEARCH (the
    # auto-registration happened, but the transition was rejected).
    with pytest.raises(InvalidTransitionError):
        manager.transition("auto_reg_2", STATE_LIVE)
    assert manager.get_state("auto_reg_2") == STATE_RESEARCH
    assert manager.get_history("auto_reg_2") == []


# ── (3) register_strategy idempotency ─────────────────────────────────────────

def test_register_strategy_idempotent(manager):
    """Registering the same strategy twice is a no-op — the second
    call returns the existing state and does NOT reset the history."""
    state1 = manager.register_strategy("idem_strat")
    manager.transition("idem_strat", STATE_EXPERIMENTAL)
    state2 = manager.register_strategy("idem_strat")  # idempotent
    assert state1 == STATE_RESEARCH
    assert state2 == STATE_EXPERIMENTAL  # current state, not initial
    # History is NOT cleared by re-registration.
    assert len(manager.get_history("idem_strat")) == 1


def test_register_strategy_with_unknown_state_raises(manager):
    """Registering with an ``initial_state`` that isn't in
    ``VALID_TRANSITIONS`` raises ``ValueError`` (caller bug — not a
    user-facing 400, an internal programming error)."""
    with pytest.raises(ValueError, match="Unknown initial lifecycle state"):
        manager.register_strategy("bad_init", initial_state="BOGUS")


def test_register_strategy_with_explicit_initial_state(manager):
    """Registering with an explicit ``initial_state`` (e.g. PAPER) sets
    the strategy's state to that value — used by tests that want to
    seed a strategy mid-pipeline."""
    manager.register_strategy("seeded", initial_state=STATE_PAPER)
    assert manager.get_state("seeded") == STATE_PAPER


# ── (4) read APIs ────────────────────────────────────────────────────────────

def test_get_state_returns_none_for_unknown(manager):
    """``get_state`` returns ``None`` (not raises) for an unknown
    strategy — mirrors the contract documented in the manager's
    docstring."""
    assert manager.get_state("never_registered") is None


def test_get_history_returns_empty_for_unknown(manager):
    """``get_history`` returns ``[]`` (not raises) for an unknown
    strategy — the API route uses this to distinguish a 404 (unknown
    strategy) from a 200 with an empty list (registered strategy with
    no transitions yet)."""
    assert manager.get_history("never_registered") == []


def test_is_terminal_only_true_for_retired(manager):
    """``is_terminal`` returns True iff the strategy is in the RETIRED
    state."""
    manager.register_strategy("term_strat")
    assert manager.is_terminal("term_strat") is False
    manager.transition("term_strat", STATE_EXPERIMENTAL)
    manager.transition("term_strat", STATE_RETIRED)
    assert manager.is_terminal("term_strat") is True
    # Unknown strategy → not terminal (None state != RETIRED).
    assert manager.is_terminal("never_registered") is False


# ── (5) LIVE-prerequisite contract ─────────────────────────────────────────────

def test_requirements_for_live_carries_documented_thresholds():
    """The ``REQUIREMENTS_FOR_LIVE`` dict carries the documented W37-2
    thresholds so an operator overriding them via construction-time
    mutation can introspect."""
    assert REQUIREMENTS_FOR_LIVE["min_sample_size"] == 30
    assert REQUIREMENTS_FOR_LIVE["min_out_of_sample_trades"] == 20
    assert REQUIREMENTS_FOR_LIVE["min_sharpe"] == 0.5
    assert REQUIREMENTS_FOR_LIVE["max_drawdown"] == 0.15
    assert REQUIREMENTS_FOR_LIVE["requires_walk_forward"] is True
    assert REQUIREMENTS_FOR_LIVE["requires_paper_validation"] is True
    assert REQUIREMENTS_FOR_LIVE["requires_approval"] is True


def test_valid_transitions_graph_matches_spec():
    """The ``VALID_TRANSITIONS`` dict carries the W37-2 spec's
    documented transition edges — no edges silently added or removed."""
    assert VALID_TRANSITIONS[STATE_RESEARCH] == [STATE_EXPERIMENTAL]
    assert VALID_TRANSITIONS[STATE_EXPERIMENTAL] == [
        STATE_BACKTESTED, STATE_RETIRED,
    ]
    assert VALID_TRANSITIONS[STATE_BACKTESTED] == [
        STATE_VALIDATED, STATE_EXPERIMENTAL, STATE_RETIRED,
    ]
    assert VALID_TRANSITIONS[STATE_VALIDATED] == [
        STATE_PAPER, STATE_BACKTESTED, STATE_RETIRED,
    ]
    assert VALID_TRANSITIONS[STATE_PAPER] == [
        STATE_LIVE_CANDIDATE, STATE_VALIDATED, STATE_SUSPENDED, STATE_RETIRED,
    ]
    assert VALID_TRANSITIONS[STATE_LIVE_CANDIDATE] == [
        STATE_LIVE, STATE_PAPER, STATE_RETIRED,
    ]
    assert VALID_TRANSITIONS[STATE_LIVE] == [
        STATE_SUSPENDED, STATE_RETIRED,
    ]
    assert VALID_TRANSITIONS[STATE_SUSPENDED] == [
        STATE_PAPER, STATE_LIVE, STATE_RETIRED,
    ]
    assert VALID_TRANSITIONS[STATE_RETIRED] == []  # Terminal


def test_all_states_covers_documented_lifecycle():
    """``ALL_STATES`` lists every state in the documented lifecycle
    (9 states total — RESEARCH / EXPERIMENTAL / BACKTESTED / VALIDATED
    / PAPER / LIVE_CANDIDATE / LIVE / SUSPENDED / RETIRED)."""
    assert len(ALL_STATES) == 9
    assert set(ALL_STATES) == {
        STATE_RESEARCH, STATE_EXPERIMENTAL, STATE_BACKTESTED,
        STATE_VALIDATED, STATE_PAPER, STATE_LIVE_CANDIDATE,
        STATE_LIVE, STATE_SUSPENDED, STATE_RETIRED,
    }


# ── (6) API routes via TestClient ─────────────────────────────────────────────

def _build_client_with_isolated_lifecycle(monkeypatch, fresh_manager):
    """Build a TestClient against the real ``api.server.app`` (so the
    ``enforce_api_auth`` middleware + auth policy is exercised
    end-to-end) while the ``strategy_lifecycle`` singleton is
    monkeypatched to a fresh instance.

    The route handlers in ``api.server`` reference ``strategy_lifecycle``
    via closure (the module-level ``from strategies.lifecycle import
    strategy_lifecycle`` import binds the singleton into the
    ``api.server`` namespace at import time), so monkeypatching BOTH:

    * ``strategies.lifecycle.strategy_lifecycle`` — so any downstream
      import that does ``from strategies.lifecycle import
      strategy_lifecycle`` (lazy / re-import) sees the fresh instance.
    * ``api.server.strategy_lifecycle`` — so the route handler
      closures (which captured the singleton at import time via the
      ``from X import Y`` form) see the fresh instance.

    ...is required. Patching only one would leave a stale reference
    in the other namespace, returning the production singleton's
    (empty) state instead of the fresh instance's evaluated state.
    Mirrors the pattern in ``tests/test_strategy_health.py``.
    """
    from api.server import app
    monkeypatch.setattr(
        "strategies.lifecycle.strategy_lifecycle", fresh_manager,
    )
    monkeypatch.setattr(
        "api.server.strategy_lifecycle", fresh_manager,
    )
    return TestClient(app, raise_server_exceptions=False), fresh_manager


def test_api_post_transition_happy_path(monkeypatch):
    """``POST /api/strategies/{name}/transition`` returns 200 + the
    new state + the audit row for a happy-path RESEARCH → EXPERIMENTAL
    transition."""
    fresh = StrategyLifecycleManager()
    client, _ = _build_client_with_isolated_lifecycle(monkeypatch, fresh)
    response = client.post(
        f"/api/strategies/{_TEST_STRATEGY_ID}/transition",
        json={"target_state": STATE_EXPERIMENTAL, "reason": "test"},
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["strategy"] == _TEST_STRATEGY_ID
    assert body["state"] == STATE_EXPERIMENTAL
    assert body["audit"]["from_state"] == STATE_RESEARCH
    assert body["audit"]["to_state"] == STATE_EXPERIMENTAL
    assert body["audit"]["reason"] == "test"


def test_api_post_transition_400_on_invalid_transition(monkeypatch):
    """``POST /api/strategies/{name}/transition`` returns 400 with a
    structured error payload when the transition is graph-invalid
    (RESEARCH → LIVE — LIVE is not in RESEARCH's allowed set)."""
    fresh = StrategyLifecycleManager()
    client, _ = _build_client_with_isolated_lifecycle(monkeypatch, fresh)
    response = client.post(
        f"/api/strategies/{_TEST_STRATEGY_ID}/transition",
        json={"target_state": STATE_LIVE},
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["detail"]["error"] == "invalid_transition"
    assert body["detail"]["from_state"] == STATE_RESEARCH
    assert body["detail"]["to_state"] == STATE_LIVE
    assert "not in allowed transitions" in body["detail"]["reason"]


def test_api_post_transition_400_on_missing_live_requirements(monkeypatch):
    """``POST /api/strategies/{name}/transition`` returns 400 when a
    LIVE promotion is missing prerequisite attestations."""
    fresh = StrategyLifecycleManager()
    client, _ = _build_client_with_isolated_lifecycle(monkeypatch, fresh)
    # Walk to LIVE_CANDIDATE first.
    for target in (
        STATE_EXPERIMENTAL, STATE_BACKTESTED, STATE_VALIDATED,
        STATE_PAPER, STATE_LIVE_CANDIDATE,
    ):
        r = client.post(
            f"/api/strategies/{_TEST_STRATEGY_ID}/transition",
            json={"target_state": target, "reason": "walk"},
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200, r.text
    # Attempt LIVE with incomplete requirements (missing requires_approval).
    reqs = dict(_FULL_LIVE_REQUIREMENTS)
    del reqs["requires_approval"]
    response = client.post(
        f"/api/strategies/{_TEST_STRATEGY_ID}/transition",
        json={
            "target_state": STATE_LIVE,
            "reason": "missing approval",
            "requirements": reqs,
        },
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["detail"]["error"] == "invalid_transition"
    assert "missing requirements" in body["detail"]["reason"]
    assert "requires_approval" in body["detail"]["reason"]


def test_api_post_transition_live_happy_path(monkeypatch):
    """``POST /api/strategies/{name}/transition`` returns 200 when a
    LIVE promotion is attempted with a complete attestation dict —
    the audit row's ``metadata`` carries the requirements."""
    fresh = StrategyLifecycleManager()
    client, _ = _build_client_with_isolated_lifecycle(monkeypatch, fresh)
    for target in (
        STATE_EXPERIMENTAL, STATE_BACKTESTED, STATE_VALIDATED,
        STATE_PAPER, STATE_LIVE_CANDIDATE,
    ):
        r = client.post(
            f"/api/strategies/{_TEST_STRATEGY_ID}/transition",
            json={"target_state": target, "reason": "walk"},
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200, r.text
    response = client.post(
        f"/api/strategies/{_TEST_STRATEGY_ID}/transition",
        json={
            "target_state": STATE_LIVE,
            "reason": "promoted to live",
            "approver": "operator",
            "requirements": _FULL_LIVE_REQUIREMENTS,
        },
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == STATE_LIVE
    assert body["audit"]["to_state"] == STATE_LIVE
    assert body["audit"]["metadata"] == _FULL_LIVE_REQUIREMENTS


def test_api_post_transition_409_on_retired_terminal(monkeypatch):
    """``POST /api/strategies/{name}/transition`` returns 409 when the
    strategy is RETIRED (terminal state — no further mutations
    allowed)."""
    fresh = StrategyLifecycleManager()
    client, _ = _build_client_with_isolated_lifecycle(monkeypatch, fresh)
    # Walk to RETIRED via the shortest path.
    for target in (STATE_EXPERIMENTAL, STATE_RETIRED):
        r = client.post(
            f"/api/strategies/{_TEST_STRATEGY_ID}/transition",
            json={"target_state": target, "reason": "retire"},
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200, r.text
    # Attempt any transition out of RETIRED → 409.
    response = client.post(
        f"/api/strategies/{_TEST_STRATEGY_ID}/transition",
        json={"target_state": STATE_LIVE, "reason": "reactivate"},
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["detail"]["error"] == "terminal_state"
    assert body["detail"]["from_state"] == STATE_RETIRED


def test_api_get_lifecycle_200_with_audit_trail(monkeypatch):
    """``GET /api/strategies/{name}/lifecycle`` returns 200 with the
    current state + the ordered audit trail + the
    ``live_requirements`` static dict."""
    fresh = StrategyLifecycleManager()
    client, _ = _build_client_with_isolated_lifecycle(monkeypatch, fresh)
    # Walk through a couple of transitions.
    for target in (STATE_EXPERIMENTAL, STATE_BACKTESTED):
        r = client.post(
            f"/api/strategies/{_TEST_STRATEGY_ID}/transition",
            json={"target_state": target, "reason": "walk"},
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200, r.text
    response = client.get(
        f"/api/strategies/{_TEST_STRATEGY_ID}/lifecycle",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["strategy"] == _TEST_STRATEGY_ID
    assert body["current_state"] == STATE_BACKTESTED
    assert len(body["history"]) == 2
    assert body["history"][0]["to_state"] == STATE_EXPERIMENTAL
    assert body["history"][1]["to_state"] == STATE_BACKTESTED
    # The static LIVE requirements are surfaced so the dashboard can
    # render the checklist next to the current state.
    assert body["live_requirements"] == REQUIREMENTS_FOR_LIVE


def test_api_get_lifecycle_404_unknown_strategy(monkeypatch):
    """``GET /api/strategies/{name}/lifecycle`` returns 404 for a
    strategy the lifecycle manager has never seen."""
    fresh = StrategyLifecycleManager()
    client, _ = _build_client_with_isolated_lifecycle(monkeypatch, fresh)
    response = client.get(
        "/api/strategies/never_registered/lifecycle",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["detail"]["error"] == "unknown_strategy"
    assert body["detail"]["strategy"] == "never_registered"


def test_api_post_transition_requires_auth(monkeypatch):
    """``POST /api/strategies/{name}/transition`` requires the bearer
    token — a missing header returns 401 (the ``enforce_api_auth``
    middleware short-circuits)."""
    fresh = StrategyLifecycleManager()
    client, _ = _build_client_with_isolated_lifecycle(monkeypatch, fresh)
    response = client.post(
        f"/api/strategies/{_TEST_STRATEGY_ID}/transition",
        json={"target_state": STATE_EXPERIMENTAL},
    )
    assert response.status_code == 401


def test_api_get_lifecycle_requires_auth(monkeypatch):
    """``GET /api/strategies/{name}/lifecycle`` also requires the
    bearer token — the auth middleware applies to every non-public
    path."""
    fresh = StrategyLifecycleManager()
    client, _ = _build_client_with_isolated_lifecycle(monkeypatch, fresh)
    response = client.get(
        f"/api/strategies/{_TEST_STRATEGY_ID}/lifecycle",
    )
    assert response.status_code == 401


# ── Module-level singleton smoke test ─────────────────────────────────────────

def test_module_singleton_importable():
    """The module-level ``strategy_lifecycle`` singleton is importable
    + carries the documented public surface (``transition``,
    ``get_state``, ``get_history``, ``validate_transition``,
    ``register_strategy``, ``is_terminal``)."""
    assert strategy_lifecycle is not None
    assert hasattr(strategy_lifecycle, "transition")
    assert hasattr(strategy_lifecycle, "get_state")
    assert hasattr(strategy_lifecycle, "get_history")
    assert hasattr(strategy_lifecycle, "validate_transition")
    assert hasattr(strategy_lifecycle, "register_strategy")
    assert hasattr(strategy_lifecycle, "is_terminal")


def test_lifecycle_audit_entry_to_dict_round_trip():
    """``LifecycleAuditEntry.to_dict`` returns a JSON-safe dict with
    every documented field — used by the API route to serialize the
    audit row."""
    entry = LifecycleAuditEntry(
        timestamp=1234567890.0,
        from_state=STATE_PAPER,
        to_state=STATE_LIVE_CANDIDATE,
        reason="paper trading successful",
        approver="operator",
        metadata={"paper_days": 14},
    )
    d = entry.to_dict()
    assert d == {
        "timestamp": 1234567890.0,
        "from_state": STATE_PAPER,
        "to_state": STATE_LIVE_CANDIDATE,
        "reason": "paper trading successful",
        "approver": "operator",
        "metadata": {"paper_days": 14},
    }
