"""
core/order_state_machine.py — Order lifecycle state machine.

Single source of truth for the canonical states a polymarket-bot order can
occupy and the legal transitions between them, backed by an append-only
SQLite history so the full progression of any order

    CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED → OPEN → FILLED

(or any rejection / expiry off-shoot) can be reconstructed after the fact
for reconciliation, audit, and the HTTP surface.

State graph
~~~~~~~~~~~~~

    CREATED ──► VALIDATED ──► SUBMITTED ──► ACKNOWLEDGED ──► OPEN ──┬──► PARTIALLY_FILLED ──► FILLED
                                                                    ├──► FILLED
                                                                    ├──► CANCELLED
                                                                    ├──► REJECTED
                                                                    └──► EXPIRED

    VALIDATED   ──► CANCELLED   (validation-stage cancellations)
    VALIDATED   ──► REJECTED    (post-validation rejections)
    SUBMITTED   ──► REJECTED    (exchange rejects before ack)
    SUBMITTED   ──► EXPIRED     (TIF elapsed without ack)
    ACKNOWLEDGED ──► CANCELLED   (cancel before resting on book)
    ACKNOWLEDGED ──► EXPIRED     (TIF elapsed while open)
    PARTIALLY_FILLED ──► PARTIALLY_FILLED (additional partial fills)
    PARTIALLY_FILLED ──► {FILLED, CANCELLED, REJECTED, EXPIRED}

Any transition NOT explicitly enumerated in ``ALLOWED_TRANSITIONS`` raises
``InvalidTransition`` — fail-closed so a stale ref to an already-terminal
order can never silently resurrect it (a FILLED order cannot move back to
OPEN, a CANCELLED order cannot move to FILLED, etc.).

Public surface
~~~~~~~~~~~~~~~

  * ``OrderState``         — str enum of canonical state names.
  * ``Order``             — frozen dataclass capturing every order field
                            used by downstream consumers.
  * ``InvalidTransition`` — raised by ``transition()`` for illegal moves.
  * ``create_order(...)``  — factory: mints a fresh ``Order`` with
                            ``state == OrderState.CREATED``.
  * ``transition(order, new_state)`` — pure: returns a new ``Order`` with
                            ``state = new_state`` (raises on illegal moves).
  * ``is_terminal(state)`` — ``True`` for FILLED / CANCELLED / REJECTED /
                            EXPIRED.
  * ``generate_idempotency_key(strategy, token_id, side, price, size)``
                          — deterministic SHA-256 of the 5-tuple so a
                            duplicate strategy decision can be detected
                            before it hits the exchange.
  * ``OrderStateMachine`` — SQLite-backed persistence layer mirroring the
                            ``DecisionLedger`` convention (append-only
                            history, ``save`` / ``load`` / ``get_history``).

The singleton ``order_state_machine`` is constructed at import time against
``DB_PATH`` (env-overridable via ``ORDER_STATE_MACHINE_DB_PATH``). The
constructor swallows init errors so the trading pipeline never crashes on a
missing / read-only data dir — same fail-soft contract as
``DecisionLedger._init_db``.
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DB_PATH = Path(
    os.environ.get("ORDER_STATE_MACHINE_DB_PATH", "/app/data/order_state_machine.db")
)


# ── Canonical lifecycle states ─────────────────────────────────────────────
class OrderState(str, enum.Enum):
    """Canonical lifecycle states for a polymarket-bot order.

    String values (rather than auto ints) so SQLite rows, JSON payloads, log
    lines, and the HTTP surface all spell the state the same way. Subclassing
    ``str`` also gives us ``OrderState.CREATED == "CREATED"`` for free —
    convenient when comparing to values read back from the DB or JSON.
    """

    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


# Terminal states — no further legal transition out of these. Centralised so
# ``is_terminal()`` and the ``ALLOWED_TRANSITIONS`` table both reference the
# same source of truth (a future addition of a new terminal state updates
# both automatically).
TERMINAL_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }
)


# Allowed transitions: from_state → set(allowed to_states).
# Built once at import time; ``transition()`` does the lookup. Every terminal
# state explicitly maps to an EMPTY frozenset so the fail-closed contract
# (illegal transition raises ``InvalidTransition``) is encoded structurally —
# no implicit "if terminal, deny" branch in ``transition()``.
ALLOWED_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset(
        {OrderState.VALIDATED, OrderState.REJECTED, OrderState.CANCELLED}
    ),
    OrderState.VALIDATED: frozenset(
        {OrderState.SUBMITTED, OrderState.REJECTED, OrderState.CANCELLED}
    ),
    OrderState.SUBMITTED: frozenset(
        {OrderState.ACKNOWLEDGED, OrderState.REJECTED, OrderState.EXPIRED, OrderState.CANCELLED}
    ),
    OrderState.ACKNOWLEDGED: frozenset(
        {OrderState.OPEN, OrderState.CANCELLED, OrderState.EXPIRED}
    ),
    OrderState.OPEN: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,  # additional partial fills
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }
    ),
    # Terminal states — no outgoing transitions (encoded explicitly so a
    # future reader doesn't have to infer "missing key ⇒ no transitions").
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.EXPIRED: frozenset(),
}


class InvalidTransition(Exception):
    """Raised when ``transition()`` is asked to move to a non-allowed state.

    Carries the from / to states so callers can log a structured rejection
    reason without re-deriving them. Fail-closed: the input ``order``'s
    state is left unchanged (``Order`` is frozen; ``transition`` is pure).
    """

    def __init__(self, from_state: OrderState, to_state: Any) -> None:
        self.from_state = from_state
        self.to_state = to_state
        from_val = from_state.value if isinstance(from_state, OrderState) else str(from_state)
        to_val = (
            to_state.value
            if isinstance(to_state, OrderState)
            else str(to_state)
        )
        super().__init__(
            f"Invalid order state transition: {from_val} -> {to_val}"
        )


# ── Order dataclass ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Order:
    """Immutable snapshot of an order at a point in time.

    Frozen so the state-machine contract is enforced structurally: callers
    must go through ``transition()`` (which returns a fresh ``Order`` via
    ``dataclasses.replace``) rather than mutating ``order.state`` in place.
    This makes the SQLite history faithful — every persisted row is a
    distinct immutable snapshot, never overwritten.

    Identity is ``order_id`` (caller-supplied or auto-minted uuid4); the
    ``idempotency_key`` is the deterministic SHA-256 over the
    (strategy, token_id, side, price, size) 5-tuple so duplicate strategy
    decisions can be de-duplicated before they hit the exchange.
    """

    order_id: str
    state: OrderState
    strategy: str
    token_id: str
    side: str
    price: float
    size: float
    idempotency_key: str
    decision_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    filled_size: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Pure helpers ────────────────────────────────────────────────────────────
def generate_idempotency_key(
    strategy: str,
    token_id: str,
    side: str,
    price: float,
    size: float,
) -> str:
    """Deterministic SHA-256 key for the (strategy, token_id, side, price, size)
    5-tuple so a duplicate strategy decision can be detected before it hits the
    exchange.

    The key is the lowercase hex SHA-256 of a pipe-delimited canonical string.
    The same inputs always produce the same key; any input perturbation
    yields a different key. ``price`` and ``size`` are formatted to 8 decimal
    places before hashing so floating-point jitter at the 1e-9 level
    (unavoidable in price math) does NOT produce spurious distinct keys for
    semantically-identical orders. ``side`` is upper-cased so ``"buy"`` and
    ``"BUY"`` collapse to the same key.
    """
    canonical = "|".join(
        [
            str(strategy or ""),
            str(token_id or ""),
            str(side or "").upper(),
            f"{float(price):.8f}",
            f"{float(size):.8f}",
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_order(
    *,
    strategy: str,
    token_id: str,
    side: str,
    price: float,
    size: float,
    decision_id: str = "",
    order_id: str | None = None,
    idempotency_key: str | None = None,
    metadata: dict[str, Any] | None = None,
    now: float | None = None,
) -> Order:
    """Factory: mint a fresh ``Order`` with ``state == OrderState.CREATED``.

    Auto-assigns ``order_id`` (uuid4, prefixed ``ord-``) and
    ``idempotency_key`` (deterministic SHA-256 over the 5-tuple) if the
    caller doesn't supply overrides. ``now`` lets tests inject a fixed
    timestamp; production callers leave it ``None`` for ``time.time()``.
    """
    ts = float(now) if now is not None else time.time()
    return Order(
        order_id=order_id or f"ord-{uuid.uuid4().hex}",
        state=OrderState.CREATED,
        strategy=strategy,
        token_id=token_id,
        side=str(side).upper(),
        price=float(price),
        size=float(size),
        idempotency_key=idempotency_key
        or generate_idempotency_key(strategy, token_id, side, price, size),
        decision_id=decision_id,
        created_at=ts,
        updated_at=ts,
        metadata=dict(metadata or {}),
    )


def is_terminal(state: OrderState) -> bool:
    """Return ``True`` for terminal states (FILLED / CANCELLED / REJECTED /
    EXPIRED), ``False`` for every other state.

    Accepts an ``OrderState`` or a plain ``str`` (in which case it's coerced
    via ``OrderState(value)`` — an unknown string returns ``False`` rather
    than raising, so a malformed state read from an external source never
    crashes the call).
    """
    if isinstance(state, OrderState):
        return state in TERMINAL_STATES
    if isinstance(state, str):
        try:
            return OrderState(state) in TERMINAL_STATES
        except ValueError:
            return False
    return False


def transition(order: Order, new_state: OrderState | str) -> Order:
    """Return a new ``Order`` with ``state == new_state`` if the transition
    is allowed; raise ``InvalidTransition`` otherwise.

    Pure — the input ``order`` is never mutated (it's frozen anyway). The
    returned ``Order`` carries the same identity (``order_id``,
    ``idempotency_key``, ``created_at``) but a bumped ``updated_at``. Every
    other field is preserved verbatim via ``dataclasses.replace``.

    Coercion: a ``str`` ``new_state`` is accepted for caller ergonomics
    (e.g. when reading the next state from a JSON config / DB row) and
    normalized to ``OrderState``. An unknown string raises
    ``InvalidTransition`` (NOT ``ValueError``) so callers only need to
    handle one exception type for all rejection reasons.
    """
    if isinstance(new_state, OrderState):
        target = new_state
    elif isinstance(new_state, str):
        try:
            target = OrderState(new_state)
        except ValueError as e:
            raise InvalidTransition(order.state, new_state) from e
    else:
        raise InvalidTransition(order.state, new_state)

    allowed = ALLOWED_TRANSITIONS.get(order.state, frozenset())
    if target not in allowed:
        raise InvalidTransition(order.state, target)

    return replace(order, state=target, updated_at=time.time())


# ── SQLite-backed persistence layer ─────────────────────────────────────────
class OrderStateMachine:
    """SQLite-backed persistence layer for ``Order`` state transitions.

    Mirrors the ``DecisionLedger`` convention: each ``save(order)`` writes
    an immutable transition row (append-only — the full history is
    preserved, never overwritten). ``load(order_id)`` returns the most
    recent snapshot; ``get_history(order_id)`` returns the ordered
    transition chain.

    Schema (single table, additive — independent SQLite db so the audit
    trail's immutability contract is not perturbed):

      order_transitions
        (id INTEGER PRIMARY KEY AUTOINCREMENT,
         timestamp REAL NOT NULL,
         order_id TEXT NOT NULL,
         state TEXT NOT NULL,
         strategy TEXT, token_id TEXT, side TEXT,
         price REAL, size REAL, filled_size REAL,
         idempotency_key TEXT, decision_id TEXT,
         metadata_json TEXT)

    Indexes on (order_id, timestamp ASC), (idempotency_key, timestamp DESC),
    and (token_id, timestamp DESC) — the first supports ``get_history`` /
    ``load``; the second supports the duplicate-detection query a future
    ``/api/orders/duplicates`` endpoint would consume; the third supports
    the per-token order listing the dashboard renders.

    The singleton ``order_state_machine`` (instantiated at import time) is
    left in its production /app/data state by tests; each test constructs a
    fresh ``OrderStateMachine(tmp_path / "test_orders.db")`` so its
    SQLite file lives under ``tmp_path`` and is hermetic.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path else DB_PATH
        self._init_db()

    # ── Schema ────────────────────────────────────────────────────────────
    def _init_db(self) -> None:
        """Create tables + indexes if absent. Safe to call on every boot."""
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS order_transitions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        order_id TEXT NOT NULL,
                        state TEXT NOT NULL,
                        strategy TEXT,
                        token_id TEXT,
                        side TEXT,
                        price REAL,
                        size REAL,
                        filled_size REAL,
                        idempotency_key TEXT,
                        decision_id TEXT,
                        metadata_json TEXT
                    )
                    """
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ord_id "
                    "ON order_transitions(order_id, timestamp ASC)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ord_idempotency "
                    "ON order_transitions(idempotency_key, timestamp DESC)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ord_token "
                    "ON order_transitions(token_id, timestamp DESC)"
                )
                conn.commit()
        except Exception as e:
            log.error(
                "[order_state_machine] Init failed (%s): %s", self._db_path, e
            )

    # ── Writes ───────────────────────────────────────────────────────────
    def save(self, order: Order) -> None:
        """Persist a single snapshot (append-only). Best-effort: errors are
        logged and swallowed — a state-machine persistence hiccup must never
        break the trading pipeline (mirrors ``DecisionLedger.record``)."""
        ts = time.time()
        metadata_json = (
            json.dumps(order.metadata, default=str) if order.metadata else None
        )
        state_val = (
            order.state.value if isinstance(order.state, OrderState) else str(order.state)
        )
        try:
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO order_transitions
                    (timestamp, order_id, state, strategy, token_id, side,
                     price, size, filled_size, idempotency_key, decision_id,
                     metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        order.order_id,
                        state_val,
                        order.strategy,
                        order.token_id,
                        order.side,
                        float(order.price),
                        float(order.size),
                        float(order.filled_size or 0.0),
                        order.idempotency_key,
                        order.decision_id,
                        metadata_json,
                    ),
                )
                conn.commit()
        except Exception as e:
            log.error(
                "[order_state_machine] save failed order_id=%s state=%s: %s",
                order.order_id,
                state_val,
                e,
            )

    # ── Reads ────────────────────────────────────────────────────────────
    def load(self, order_id: str) -> Order | None:
        """Return the latest snapshot for ``order_id``, or ``None`` if no rows
        exist for that id."""
        if not order_id:
            return None
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT timestamp, order_id, state, strategy, token_id,
                           side, price, size, filled_size, idempotency_key,
                           decision_id, metadata_json
                    FROM order_transitions
                    WHERE order_id = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT 1
                    """,
                    (order_id,),
                )
                row = cursor.fetchone()
        except Exception as e:
            log.error(
                "[order_state_machine] load failed order_id=%s: %s",
                order_id,
                e,
            )
            return None
        if row is None:
            return None
        return _row_to_order(row)

    def get_history(self, order_id: str) -> list[Order]:
        """Return every persisted snapshot for ``order_id``, oldest-first
        (the chronological order in which ``save`` was called)."""
        if not order_id:
            return []
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT timestamp, order_id, state, strategy, token_id,
                           side, price, size, filled_size, idempotency_key,
                           decision_id, metadata_json
                    FROM order_transitions
                    WHERE order_id = ?
                    ORDER BY timestamp ASC, id ASC
                    """,
                    (order_id,),
                )
                rows = cursor.fetchall()
        except Exception as e:
            log.error(
                "[order_state_machine] get_history failed order_id=%s: %s",
                order_id,
                e,
            )
            return []
        return [_row_to_order(r) for r in rows]


def _row_to_order(row: sqlite3.Row) -> Order:
    """Convert a SQLite row into an ``Order`` snapshot.

    ``created_at`` and ``updated_at`` are both set to the row's
    ``timestamp`` because each persisted row is an *immutable snapshot*
    of a single transition event — the "creation time" of this snapshot
    and the "update time" of this snapshot are the same thing. The full
    chain of transitions is recoverable via ``get_history(order_id)``.
    """
    metadata: dict[str, Any] = {}
    raw = row["metadata_json"]
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                metadata = decoded
        except Exception:
            metadata = {}
    ts = float(row["timestamp"])
    return Order(
        order_id=row["order_id"],
        state=OrderState(row["state"]),
        strategy=row["strategy"] or "",
        token_id=row["token_id"] or "",
        side=row["side"] or "",
        price=float(row["price"] or 0.0),
        size=float(row["size"] or 0.0),
        idempotency_key=row["idempotency_key"] or "",
        decision_id=row["decision_id"] or "",
        created_at=ts,
        updated_at=ts,
        filled_size=float(row["filled_size"] or 0.0),
        metadata=metadata,
    )


# Module-level singleton (mirrors ``decision_ledger`` / ``audit_logger``
# convention so importers can grab the instance at module import time).
order_state_machine = OrderStateMachine()


__all__ = [
    "DB_PATH",
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATES",
    "OrderState",
    "Order",
    "InvalidTransition",
    "OrderStateMachine",
    "order_state_machine",
    "create_order",
    "transition",
    "is_terminal",
    "generate_idempotency_key",
]
