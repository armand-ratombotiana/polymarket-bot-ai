"""
Unit tests for ``core/order_state_machine.py``.

U6 — Order state machine unit tests.

Covers the seven behaviours required by the task spec:

  1. ``create_order(...)`` returns an ``Order`` with
     ``state == OrderState.CREATED`` (and the idempotency_key is auto-minted
     deterministically from the 5-tuple when the caller doesn't supply one).
  2. ``transition(order, OrderState.VALIDATED)`` succeeds for an order
     currently in ``CREATED`` — the returned ``Order`` has the new state and
     the input ``order`` is left untouched (purity contract).
  3. ``transition(order, OrderState.OPEN)`` raises ``InvalidTransition``
     when the order is already in the terminal ``FILLED`` state —
     fail-closed so a stale ref to a completed order cannot resurrect it.
  4. ``is_terminal()`` returns ``True`` for FILLED and CANCELLED.
  5. ``is_terminal()`` returns ``False`` for OPEN (and every other non-
     terminal state — covered parametrically).
  6. ``generate_idempotency_key(...)`` is deterministic — the same inputs
     always produce the same key; any input perturbation produces a
     different key.
  7. Full happy path CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED →
     OPEN → FILLED — every transition succeeds, every snapshot is
     persisted to a temp SQLite DB via ``OrderStateMachine.save()``, and
     ``get_history(order_id)`` returns the ordered chain.

The state machine's persistence layer (``OrderStateMachine``) reads its
DB path from a module-level ``DB_PATH`` constant (env-overridable via
``ORDER_STATE_MACHINE_DB_PATH``) at construction time. Each test that
touches SQLite constructs a fresh ``OrderStateMachine(tmp_path /
"test_orders.db")`` so the production singleton (built at import time
against the non-writable ``/app/data`` sandbox path) is left untouched.
Mirrors the isolation pattern established by ``tests/test_decision_ledger.py``
(S9) and ``tests/test_closed_positions.py`` (T11).

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (the repo's ``pytest.ini`` /
``pyproject.toml`` are not edited per the U6 "do not edit existing files"
convention, so ``asyncio_mode = "auto"`` cannot be enabled via config —
mirrors every sibling ``tests/test_*.py``).
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# ── Redirect ORDER_STATE_MACHINE_DB_PATH to /tmp BEFORE importing the
# module. The singleton ``order_state_machine`` is constructed at import
# time and reads its DB path from this env var (falling back to
# ``/app/data/order_state_machine.db`` — unwritable in the sandbox).
# Redirecting keeps the import-time ``_init_db`` call hermetic — it never
# touches the production path, even if the sandbox mounts ``/app/data``
# writable. ``setdefault`` lets an outer runner / sibling test file override
# if it needs to (mirrors ``tests/test_observability.py`` lines 59-67).
_TMP_ROOT = Path("/tmp/order_state_machine_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault(
    "ORDER_STATE_MACHINE_DB_PATH", str(_TMP_ROOT / "order_state_machine.db")
)

# Make the polymarket-bot package root importable as top-level modules
# (``core.order_state_machine``) regardless of the cwd pytest was launched
# from. Mirrors the bootstrap pattern in every sibling ``test_*.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.order_state_machine import (  # noqa: E402
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    InvalidTransition,
    Order,
    OrderStateMachine,
    OrderState,
    create_order,
    generate_idempotency_key,
    is_terminal,
    transition,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` declares ``testpaths = tests`` and the
# project's pytest-asyncio is in STRICT mode (no ``asyncio_mode = "auto"``);
# per the U6 task constraint we cannot edit pytest.ini / pyproject.toml,
# so we use the module-level ``pytestmark`` idiom instead (mirrors every
# sibling ``test_*.py``).
pytestmark = pytest.mark.asyncio


# ── Fixture: fresh temp-DB-backed state machine per test ───────────────────
@pytest.fixture
def machine(tmp_path):
    """Return an ``OrderStateMachine`` whose SQLite file lives under
    ``tmp_path``.

    Passing an explicit ``db_path`` to the constructor bypasses the
    module-level ``DB_PATH`` lookup that the production singleton uses, so
    the import-time singleton (built against the non-writable
    ``/app/data/order_state_machine.db``) is never touched. Mirrors the
    ``isolated_decision_ledger`` fixture in ``tests/conftest.py``.
    """
    return OrderStateMachine(tmp_path / "test_orders.db")


# ── 1. create_order() returns an Order in CREATED ──────────────────────────
async def test_create_order_returns_order_in_CREATED_state():
    """``create_order`` must mint a fresh ``Order`` whose ``state`` is
    ``OrderState.CREATED`` (the canonical entry-point state of the state
    machine) and whose identity fields are populated.

    Belt-and-braces: also verifies the auto-minted ``idempotency_key`` matches
    a stand-alone call to ``generate_idempotency_key`` with the same 5-tuple
    (proves the factory actually delegates to that helper rather than
    silently generating a UUID-style random key).
    """
    order = create_order(
        strategy="ml_sig_v1",
        token_id="TOK_ABC",
        side="BUY",
        price=0.55,
        size=10.0,
    )

    # (a) State machine entry-point contract: CREATED.
    assert order.state is OrderState.CREATED
    assert order.state == OrderState.CREATED  # str-enum equality

    # (b) Identity / payload fields preserved verbatim.
    assert order.strategy == "ml_sig_v1"
    assert order.token_id == "TOK_ABC"
    assert order.side == "BUY"  # upper-cased by create_order
    assert order.price == pytest.approx(0.55)
    assert order.size == pytest.approx(10.0)

    # (c) order_id auto-minted with the canonical prefix.
    assert order.order_id.startswith("ord-")
    assert len(order.order_id) == 4 + 32  # "ord-" + uuid4().hex

    # (d) idempotency_key auto-minted and matches the deterministic helper
    # over the same 5-tuple (proves the factory delegates to
    # generate_idempotency_key rather than rolling its own random key).
    expected_key = generate_idempotency_key(
        "ml_sig_v1", "TOK_ABC", "BUY", 0.55, 10.0
    )
    assert order.idempotency_key == expected_key
    assert len(order.idempotency_key) == 64  # SHA-256 hex digest

    # (e) created_at / updated_at are populated (and equal at creation time).
    assert order.created_at > 0
    assert order.updated_at == order.created_at

    # (f) Optional fields default to their documented empty values.
    assert order.decision_id == ""
    assert order.filled_size == 0.0
    assert order.metadata == {}


# ── 2. transition CREATED → VALIDATED succeeds ────────────────────────────
async def test_transition_CREATED_to_VALIDATED_succeeds():
    """``transition`` must accept CREATED → VALIDATED (the canonical first hop
    of the happy path) and return a NEW ``Order`` whose ``state`` is
    VALIDATED. The input ``order`` must remain unchanged — the ``Order``
    dataclass is frozen and ``transition`` is pure (returns a fresh instance
    via ``dataclasses.replace``).
    """
    created = create_order(
        strategy="ml_sig_v1",
        token_id="TOK_VAL",
        side="BUY",
        price=0.42,
        size=5.0,
    )
    assert created.state is OrderState.CREATED

    validated = transition(created, OrderState.VALIDATED)

    # (a) The new state landed on the returned Order.
    assert validated.state is OrderState.VALIDATED

    # (b) Identity / payload fields preserved across the transition
    #     (transition must NOT mutate strategy / token_id / side / price /
    #     size / idempotency_key / order_id / decision_id).
    assert validated.order_id == created.order_id
    assert validated.idempotency_key == created.idempotency_key
    assert validated.strategy == created.strategy
    assert validated.token_id == created.token_id
    assert validated.side == created.side
    assert validated.price == pytest.approx(created.price)
    assert validated.size == pytest.approx(created.size)
    assert validated.decision_id == created.decision_id

    # (c) created_at preserved, updated_at bumped forward (transition stamps
    #     a fresh ``time.time()`` on every successful hop).
    assert validated.created_at == created.created_at
    assert validated.updated_at >= created.updated_at

    # (d) Purity: the input order is unchanged (frozen dataclass — verify both
    #     the state and the equality contract).
    assert created.state is OrderState.CREATED  # input untouched
    assert created != validated  # different state ⇒ not equal

    # (e) Belt-and-braces: the transition is also legal via the str form
    #     (ergonomics for callers reading the next state from JSON / DB).
    created_again = create_order(
        strategy="ml_sig_v1", token_id="TOK_VAL2",
        side="BUY", price=0.42, size=5.0,
    )
    validated_via_str = transition(created_again, "VALIDATED")
    assert validated_via_str.state is OrderState.VALIDATED


# ── 3. transition FILLED → OPEN raises InvalidTransition ───────────────────
async def test_transition_FILLED_to_OPEN_raises_InvalidTransition():
    """``transition`` must raise ``InvalidTransition`` when asked to move a
    terminal-state order to a non-legal state. ``FILLED`` is terminal — its
    allowed-transitions set is empty — so OPEN is forbidden.

    Belt-and-braces:
      * The input order's state is left unchanged (fail-closed).
      * The exception carries both the from and to states on its public
        attributes so callers can log a structured rejection reason.
    """
    # Stage an order in FILLED via the legal happy path (rather than mocking
    # state directly — proves the order actually reaches FILLED through the
    # transition function, which is the same code path production uses).
    order = create_order(
        strategy="mm_v1", token_id="TOK_F", side="SELL",
        price=0.71, size=4.0,
    )
    for nxt in (
        OrderState.VALIDATED,
        OrderState.SUBMITTED,
        OrderState.ACKNOWLEDGED,
        OrderState.OPEN,
        OrderState.FILLED,
    ):
        order = transition(order, nxt)
    assert order.state is OrderState.FILLED
    assert is_terminal(order.state) is True

    # Sanity: FILLED has no outgoing transitions in the table.
    assert ALLOWED_TRANSITIONS[OrderState.FILLED] == frozenset()

    # The illegal move raises InvalidTransition (NOT e.g. ValueError).
    with pytest.raises(InvalidTransition) as excinfo:
        transition(order, OrderState.OPEN)

    # The exception carries both states for structured logging.
    assert excinfo.value.from_state is OrderState.FILLED
    assert excinfo.value.to_state is OrderState.OPEN
    # The message names both states (uses the .value for human-readability).
    assert "FILLED" in str(excinfo.value)
    assert "OPEN" in str(excinfo.value)

    # Fail-closed: the input order's state is unchanged after the raise.
    assert order.state is OrderState.FILLED

    # The illegal reverse-direction (FILLED → VALIDATED etc.) also raises —
    # parametric check that the empty allowed-set is the gate, not a
    # special-case branch for OPEN.
    for illegal in (
        OrderState.CREATED,
        OrderState.VALIDATED,
        OrderState.SUBMITTED,
        OrderState.ACKNOWLEDGED,
        OrderState.OPEN,
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
        OrderState.FILLED,  # self-transition also forbidden (not in allowed set)
    ):
        with pytest.raises(InvalidTransition):
            transition(order, illegal)


# ── 4. is_terminal() returns True for FILLED and CANCELLED ─────────────────
@pytest.mark.parametrize("state", [OrderState.FILLED, OrderState.CANCELLED])
async def test_is_terminal_returns_True_for_FILLED_and_CANCELLED(state):
    """``is_terminal`` must return ``True`` for every state in
    ``TERMINAL_STATES``. The task spec calls out FILLED and CANCELLED
    explicitly — parametrised so a failure for either is visible
    independently in the test report.
    """
    assert is_terminal(state) is True
    # Belt-and-braces: also accept the str form (ergonomics for callers
    # reading state from JSON / DB rows).
    assert is_terminal(state.value) is True
    # The state is also in the canonical TERMINAL_STATES set (the single
    # source of truth the function consults).
    assert state in TERMINAL_STATES


# ── 5. is_terminal() returns False for OPEN ───────────────────────────────
async def test_is_terminal_returns_False_for_OPEN():
    """``is_terminal`` must return ``False`` for ``OPEN`` — OPEN is the
    canonical resting-on-book state from which an order can transition to
    FILLED / CANCELLED / REJECTED / EXPIRED, so it is by definition NOT
    terminal.

    Belt-and-braces: also checks every other non-terminal state in the
    enum (CREATED / VALIDATED / SUBMITTED / ACKNOWLEDGED / PARTIALLY_FILLED)
    so the test catches a future regression where a state is accidentally
    added to ``TERMINAL_STATES``.
    """
    assert is_terminal(OrderState.OPEN) is False
    assert is_terminal("OPEN") is False  # str form

    # Every non-terminal state in the enum also returns False.
    non_terminal = [
        OrderState.CREATED,
        OrderState.VALIDATED,
        OrderState.SUBMITTED,
        OrderState.ACKNOWLEDGED,
        OrderState.OPEN,
        OrderState.PARTIALLY_FILLED,
    ]
    for s in non_terminal:
        assert is_terminal(s) is False, f"{s} should not be terminal"
        assert s not in TERMINAL_STATES

    # Sanity: the two sets partition the enum exactly (no overlap, no gap).
    assert TERMINAL_STATES.isdisjoint(non_terminal)
    assert len(TERMINAL_STATES) + len(non_terminal) == len(OrderState)


# ── 6. generate_idempotency_key() is deterministic ─────────────────────────
async def test_generate_idempotency_key_is_deterministic():
    """``generate_idempotency_key`` must be deterministic — the same
    (strategy, token_id, side, price, size) 5-tuple must always produce the
    same key. Any perturbation of any of the 5 inputs must produce a
    different key (so a duplicate strategy decision can be detected before
    it hits the exchange).
    """
    key_a = generate_idempotency_key(
        "ml_sig_v1", "TOK_X", "BUY", 0.55, 10.0
    )
    key_b = generate_idempotency_key(
        "ml_sig_v1", "TOK_X", "BUY", 0.55, 10.0
    )

    # Determinism: identical inputs → identical key.
    assert key_a == key_b

    # SHA-256 hex shape (64 lowercase hex chars).
    assert len(key_a) == 64
    assert all(c in "0123456789abcdef" for c in key_a)

    # Perturbation: changing ANY of the 5 inputs yields a different key.
    assert generate_idempotency_key(
        "ml_sig_v2", "TOK_X", "BUY", 0.55, 10.0
    ) != key_a  # strategy
    assert generate_idempotency_key(
        "ml_sig_v1", "TOK_Y", "BUY", 0.55, 10.0
    ) != key_a  # token_id
    assert generate_idempotency_key(
        "ml_sig_v1", "TOK_X", "SELL", 0.55, 10.0
    ) != key_a  # side
    assert generate_idempotency_key(
        "ml_sig_v1", "TOK_X", "BUY", 0.56, 10.0
    ) != key_a  # price
    assert generate_idempotency_key(
        "ml_sig_v1", "TOK_X", "BUY", 0.55, 11.0
    ) != key_a  # size

    # Case-insensitivity contract on ``side``: "buy" and "BUY" collapse to
    # the same key (the implementation upper-cases ``side`` before hashing
    # so a caller passing either form de-duplicates correctly).
    assert generate_idempotency_key(
        "ml_sig_v1", "TOK_X", "buy", 0.55, 10.0
    ) == key_a

    # Floating-point stability: values within 1e-9 of the canonical price /
    # size (sub-8dp jitter) collapse to the same key — the implementation
    # formats to 8 decimal places before hashing so 0.5500000001 and 0.55
    # both round to "0.55000000" and produce the same key.
    assert generate_idempotency_key(
        "ml_sig_v1", "TOK_X", "BUY", 0.55 + 1e-9, 10.0
    ) == key_a
    assert generate_idempotency_key(
        "ml_sig_v1", "TOK_X", "BUY", 0.55, 10.0 + 1e-9
    ) == key_a


# ── 7. Full happy path CREATED → ... → FILLED ─────────────────────────────
async def test_full_happy_path_CREATED_to_FILLED_with_temp_db(machine):
    """Drive the full canonical happy path end-to-end and verify each
    transition succeeds, each snapshot is persisted to the temp SQLite DB,
    and ``get_history(order_id)`` returns the ordered chain.

    Happy path under test (6 transitions, 7 snapshots including CREATED):

        CREATED
          → VALIDATED
          → SUBMITTED
          → ACKNOWLEDGED
          → OPEN
          → PARTIALLY_FILLED   (proves OPEN→PARTIAL is legal)
          → FILLED             (terminal — proves PARTIAL→FILLED is legal)

    Each snapshot is persisted via ``machine.save(order)`` between hops so
    the SQLite ``order_transitions`` table holds the full transition chain
    and ``get_history`` can reconstruct it. The final state is FILLED so
    ``is_terminal`` flips True exactly once at the end.
    """
    # ── Stage 0: create the order in CREATED and persist the seed snapshot.
    order = create_order(
        strategy="ml_sig_v1",
        token_id="TOK_HP",
        side="BUY",
        price=0.48,
        size=20.0,
        decision_id="dec-happy-1",
    )
    machine.save(order)

    # The happy path: each transition is legal from the prior state.
    happy_path = [
        OrderState.VALIDATED,
        OrderState.SUBMITTED,
        OrderState.ACKNOWLEDGED,
        OrderState.OPEN,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    ]

    # Track the expected state sequence (incl. the seed CREATED state) for
    # the post-loop history assertion.
    expected_states = [OrderState.CREATED] + happy_path

    # ── Drive the happy path, persisting every snapshot.
    for nxt in happy_path:
        # Pre-condition: this transition is legal from the current state.
        assert nxt in ALLOWED_TRANSITIONS[order.state], (
            f"transition {order.state} -> {nxt} must be in ALLOWED_TRANSITIONS"
        )
        # Apply the transition (pure — returns a fresh Order).
        order = transition(order, nxt)
        # Persist the new snapshot to the temp DB.
        machine.save(order)
        # Tiny sleep so each row lands at a strictly greater SQLite REAL
        # timestamp (~µs precision; 5ms is a comfortable margin even on a
        # heavily-loaded CI box). Mirrors test_decision_ledger.py L150.
        await asyncio.sleep(0.005)

    # ── Post-loop invariants on the in-memory order.
    assert order.state is OrderState.FILLED
    assert is_terminal(order.state) is True
    # Identity preserved end-to-end.
    assert order.order_id.startswith("ord-")
    assert order.idempotency_key == generate_idempotency_key(
        "ml_sig_v1", "TOK_HP", "BUY", 0.48, 20.0
    )
    # updated_at advanced past created_at (at least one transition happened).
    assert order.updated_at >= order.created_at

    # ── SQLite round-trip: load() returns the latest snapshot.
    latest = machine.load(order.order_id)
    assert latest is not None
    assert latest.state is OrderState.FILLED
    assert latest.order_id == order.order_id
    assert latest.idempotency_key == order.idempotency_key
    assert latest.strategy == order.strategy
    assert latest.token_id == order.token_id
    assert latest.side == order.side
    assert latest.price == pytest.approx(order.price)
    assert latest.size == pytest.approx(order.size)
    assert latest.decision_id == order.decision_id

    # ── SQLite round-trip: get_history() returns the full ordered chain.
    history = machine.get_history(order.order_id)
    assert len(history) == len(expected_states)  # one row per save()

    # States match the expected sequence, in chronological order.
    assert [h.state for h in history] == expected_states

    # Every snapshot shares the same identity (order_id / idempotency_key).
    assert all(h.order_id == order.order_id for h in history)
    assert all(h.idempotency_key == order.idempotency_key for h in history)

    # Timestamps are monotonically non-decreasing across the chain.
    timestamps = [h.updated_at for h in history]
    assert timestamps == sorted(timestamps)

    # The first snapshot is CREATED; the last is FILLED (terminal).
    assert history[0].state is OrderState.CREATED
    assert history[-1].state is OrderState.FILLED
    assert is_terminal(history[-1].state) is True

    # ── Idempotency: the load()ed FILLED snapshot CANNOT transition further
    # (terminal) — every post-FILLED transition raises InvalidTransition.
    with pytest.raises(InvalidTransition):
        transition(latest, OrderState.OPEN)
    with pytest.raises(InvalidTransition):
        transition(latest, OrderState.CANCELLED)

    # ── Empty-id / unknown-id guards return empty rather than raising.
    assert machine.load("") is None
    assert machine.load("ord-nonexistent") is None
    assert machine.get_history("") == []
    assert machine.get_history("ord-nonexistent") == []
