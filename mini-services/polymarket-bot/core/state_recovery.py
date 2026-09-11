"""
core/state_recovery.py — State recovery manager (W24-1).

Restores system state after a restart so the bot survives reboots without:

  * losing open positions
  * duplicating orders (resubmitting orders already placed)
  * losing track of pending fills
  * corrupting the decision ledger chain

On startup the singleton ``state_recovery.recover()`` is awaited inside the
FastAPI lifespan handler. It loads the last known checkpoint from
``RECOVERY_STATE_PATH`` (a JSON file distinct from ``STORE_STATE_PATH`` —
``data_store.py`` already owns that file with its own schema), inspects the
in-memory store / kill-switch / feature-flag subsystems, and produces a
``RecoveryReport`` summarising what was recovered + what needs reconciliation.

Periodic ``checkpoint()`` calls (every 30 s by the lifespan ``checkpoint_loop``
background task) snapshot the live ``store`` state (open positions + open
orders + paper balance) alongside the durable kill-switch file marker and the
SQLite-backed feature flags so the next restart can rebuild an accurate
"what was open at shutdown?" picture.

Why a separate file from ``data_store.STATE_FILE``
---------------------------------------------------
``core.data_store.DataStore.save_to_disk()`` already persists positions /
trades / equity_history / paper_balance / peak_equity to
``STORE_STATE_PATH`` (default ``/app/data/store_state.json``) and
``load_from_disk()`` already restores them on import. ``state_recovery`` does
NOT re-implement that persistence — it adds the two pieces ``data_store``
does NOT persist:

  1. **Open orders** — the bot's in-memory ``store.open_orders`` dict is
     wiped on restart. Without snapshotting it, the recovery manager has no
     way to know "the bot had an open BUY order at shutdown" so it cannot
     flag the order as stale (potentially filled during downtime).
  2. **Kill-switch + feature-flag accounting** — these are already durable
     (kill-switch is a file marker, feature-flags are SQLite), but the
     recovery report bundles their status into a single operator-facing
     view so an operator doesn't have to grep three subsystems to verify
     "is everything in the state I expect?".

Public surface
~~~~~~~~~~~~~~

  * ``RecoveryReport``             — dataclass returned by ``recover()``.
  * ``StateRecoveryManager``       — async recovery + checkpoint manager.
  * ``state_recovery``             — module-level singleton consumed by the
                                      FastAPI lifespan + ``GET /api/system/
                                      recovery-report`` endpoint.

The singleton is constructed against ``RECOVERY_STATE_PATH`` (env-overridable;
default ``/app/data/recovery_state.json``) so production callers share the
same on-disk file. Tests construct a fresh ``StateRecoveryManager(tmp_path /
"recovery_state.json")`` against a ``tmp_path`` JSON file for full isolation.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _to_str(v: Any) -> str:
    """Coerce a value to its canonical string form.

    Handles ``Enum`` members (uses ``.value`` so ``Side.BUY`` → ``"BUY"``,
    NOT the default ``str(Enum)`` form ``"Side.BUY"``) and falls back to
    ``str(v)`` for plain strings / numbers. ``None`` → ``""`` so a missing
    dataclass field never serialises as the string ``"None"``.
    """
    if v is None:
        return ""
    if isinstance(v, Enum):
        return str(v.value)
    return str(v)

# Distinct from ``core.data_store.STATE_FILE`` (``STORE_STATE_PATH``) — see
# module docstring "Why a separate file" section. ``setdefault`` is NOT used
# here because the manager is constructed at module-import time and the
# caller (conftest.py for tests, Docker env for prod) MUST set this BEFORE
# import; otherwise the singleton would silently pick up the production path
# inside a test sandbox.
RECOVERY_STATE_PATH = Path(
    os.environ.get("RECOVERY_STATE_PATH", "/app/data/recovery_state.json")
)

# Statuses that mark an order as "potentially stale" on recover — i.e. the
# order was open at shutdown and may have filled during downtime, so the
# reconciliation engine should re-query the exchange before any new order is
# submitted with the same idempotency key. Mirrors the OSM ``OrderState``
# non-terminal states (``CREATED`` / ``VALIDATED`` / ``SUBMITTED`` /
# ``ACKNOWLEDGED`` / ``OPEN`` / ``PARTIALLY_FILLED``) plus the legacy
# data_store ``OrderStatus`` non-terminal values (``OPEN`` /
# ``PARTIALLY_FILLED``) — a superset so the same constant works for both
# subsystems regardless of which produced the snapshot.
STALE_ORDER_STATUSES: frozenset[str] = frozenset(
    {
        "PENDING",
        "OPEN",
        "PARTIALLY_FILLED",
        # OSM-only non-terminal states — included so a future checkpoint
        # format that persists OSM snapshots instead of / alongside the
        # data_store's open_orders dict still classifies them as stale.
        "CREATED",
        "VALIDATED",
        "SUBMITTED",
        "ACKNOWLEDGED",
    }
)


@dataclass
class RecoveryReport:
    """Summary of the most recent ``recover()`` invocation.

    Surfaced verbatim by the ``GET /api/system/recovery-report`` endpoint so
    an operator (or a future ``RecoveryReportPanel`` React component) can
    verify "the bot booted with N positions + M open orders + K stale orders
    + kill switch off" without grepping server logs.
    """

    recovered_positions: int = 0
    recovered_orders: int = 0
    stale_orders: int = 0
    kill_switch_active: bool = False
    flags_restored: int = 0
    recovery_time: float = 0.0
    errors: list[str] = field(default_factory=list)
    # W24-1 — when the recovery ran + the timestamp of the checkpoint the
    # report was rebuilt from. ``checkpoint_timestamp`` is ``None`` when
    # the manager recovered with no on-disk state (fresh boot) so the API
    # endpoint can distinguish "no checkpoint exists" from "checkpoint
    # exists but its timestamp was 0".
    recovered_at: float = field(default_factory=time.time)
    checkpoint_timestamp: Optional[float] = None


class StateRecoveryManager:
    """Manages state recovery after restart.

    Lifecycle
    ~~~~~~~~~
      1. The FastAPI lifespan ``startup`` phase awaits ``recover()`` — this
         loads the last checkpoint (if any), classifies open orders as
         stale-vs-terminal, and stores the resulting ``RecoveryReport`` on
         ``self._last_report`` so the ``GET /api/system/recovery-report``
         endpoint can surface it.
      2. A background ``checkpoint_loop`` (started by the lifespan) awaits
         ``checkpoint()`` every 30 s — this snapshots the live ``store``
         state to ``RECOVERY_STATE_PATH`` so the next restart has a fresh
         picture.
      3. The lifespan ``shutdown`` phase awaits ``checkpoint()`` one final
         time so the file reflects the exact pre-shutdown state (not up to
         30 s stale).

    Failure mode
    ~~~~~~~~~~~~
    Every step is best-effort: a corrupt / missing / unreadable checkpoint
    file yields a ``RecoveryReport`` with ``errors=[...]`` populated but
    does NOT raise — the bot must always be able to boot (even if recovery
    is partial), mirroring the fail-soft contract of every other singleton
    in the codebase (``DecisionLedger._init_db`` /
    ``OrderStateMachine._init_db`` / ``FeatureFlagManager._init_db``).
    """

    def __init__(self, state_path: Optional[Path] = None) -> None:
        # Allow tests to pass an explicit ``Path`` so they don't have to
        # mutate the module global / env var. Production code uses the
        # default ``RECOVERY_STATE_PATH`` resolved at import time.
        self._state_path: Path = Path(state_path) if state_path else RECOVERY_STATE_PATH
        # ``_last_report`` is populated by ``recover()`` and surfaced by the
        # ``GET /api/system/recovery-report`` endpoint. ``None`` until the
        # first ``recover()`` call so the endpoint can distinguish "no
        # recovery yet" (e.g. queried before lifespan startup ran) from a
        # legitimate empty recovery (fresh boot).
        self._last_report: Optional[RecoveryReport] = None

    # ── Public API ────────────────────────────────────────────────────────

    async def recover(self) -> RecoveryReport:
        """Perform full state recovery.

        Loads the last checkpoint from ``self._state_path`` (if present),
        classifies any open orders as stale (they may have filled during
        downtime), restores the kill-switch / feature-flag accounting, and
        stores the resulting ``RecoveryReport`` on ``self._last_report``.

        Returns the report so the lifespan startup handler can log it.
        """
        start = time.time()
        errors: list[str] = []

        # ── 1. Load persisted state ──
        state = self._load_state()
        if state is None:
            logger.warning(
                "[state_recovery] No persisted state found at %s — starting fresh",
                self._state_path,
            )
            # Still probe the live subsystems so the report is accurate for
            # the kill-switch / feature-flag dimensions even on a fresh boot.
            kill_switch_active = self._probe_kill_switch()
            flags_restored = self._probe_flag_count()
            report = RecoveryReport(
                recovered_positions=0,
                recovered_orders=0,
                stale_orders=0,
                kill_switch_active=kill_switch_active,
                flags_restored=flags_restored,
                recovery_time=time.time() - start,
                errors=errors,
                checkpoint_timestamp=None,
            )
            self._last_report = report
            logger.info(
                "[state_recovery] Fresh boot — kill_switch=%s, flags=%d, recovery_time=%.3fs",
                kill_switch_active,
                flags_restored,
                report.recovery_time,
            )
            return report

        checkpoint_ts = state.get("timestamp")
        try:
            checkpoint_ts_float = float(checkpoint_ts) if checkpoint_ts is not None else None
        except (TypeError, ValueError):
            checkpoint_ts_float = None
            errors.append(
                f"checkpoint timestamp not a number: {checkpoint_ts!r}"
            )

        # ── 2. Restore positions ──
        positions = state.get("positions", [])
        if not isinstance(positions, list):
            errors.append(f"positions not a list: {type(positions).__name__}")
            positions = []
        logger.info("[state_recovery] Recovering %d positions", len(positions))

        # ── 3. Restore orders ──
        orders = state.get("orders", [])
        if not isinstance(orders, list):
            errors.append(f"orders not a list: {type(orders).__name__}")
            orders = []
        logger.info("[state_recovery] Recovering %d orders", len(orders))

        # ── 4. Detect stale orders ──
        # Open orders at shutdown may have filled during downtime — flag
        # them so the reconciliation engine (W24 follow-up) knows which
        # orders to re-query the exchange for before any new order is
        # submitted with the same idempotency key. Without this, a restart
        # could re-submit an order that already filled → duplicate fill.
        stale_orders = self._find_stale_orders(orders)
        if stale_orders:
            logger.warning(
                "[state_recovery] Found %d potentially stale orders — "
                "need reconciliation (statuses: %s)",
                len(stale_orders),
                sorted({str(o.get("status", "?")) for o in stale_orders}),
            )

        # ── 5. Restore kill-switch state ──
        # The kill switch is file-backed (``core.safety.write_kill_switch``
        # writes ``/app/data/kill_switch``); the file survives restarts
        # naturally. We probe the live file rather than trusting the
        # checkpoint's ``kill_switch_active`` field so an operator who
        # toggled the switch via the file system between shutdown and boot
        # is respected.
        kill_switch_active = self._probe_kill_switch()
        if kill_switch_active:
            logger.warning(
                "[state_recovery] Kill switch is active — maintaining halt state"
            )

        # ── 6. Restore feature-flag state ──
        # Feature flags are SQLite-backed (``FLAGS_DB_PATH``); the table
        # survives restarts naturally. We probe the live cache so the
        # report reflects any flag toggled via the API between shutdown
        # and boot (rather than the stale checkpoint snapshot).
        flags_restored = self._probe_flag_count()
        # If the checkpoint captured a flag snapshot, log any divergence
        # between the checkpoint and the live cache for operator visibility.
        checkpoint_flags = state.get("feature_flags", {})
        if isinstance(checkpoint_flags, dict) and checkpoint_flags:
            logger.info(
                "[state_recovery] Feature flags: %d in checkpoint, %d live",
                len(checkpoint_flags),
                flags_restored,
            )

        recovery_time = time.time() - start
        logger.info(
            "[state_recovery] State recovery complete in %.3fs "
            "(positions=%d, orders=%d, stale=%d, kill_switch=%s, flags=%d)",
            recovery_time,
            len(positions),
            len(orders),
            len(stale_orders),
            kill_switch_active,
            flags_restored,
        )

        report = RecoveryReport(
            recovered_positions=len(positions),
            recovered_orders=len(orders),
            stale_orders=len(stale_orders),
            kill_switch_active=kill_switch_active,
            flags_restored=flags_restored,
            recovery_time=recovery_time,
            errors=errors,
            checkpoint_timestamp=checkpoint_ts_float,
        )
        self._last_report = report
        return report

    async def checkpoint(self) -> None:
        """Save current state for recovery.

        Snapshots the live ``store`` state (open positions, open orders,
        paper balance), the durable kill-switch file marker, and the
        SQLite-backed feature-flag cache to ``self._state_path`` so the next
        restart's ``recover()`` can rebuild an accurate "what was open at
        shutdown?" picture.

        Atomic write: writes to ``<path>.tmp`` then ``replace``s the target
        so a crash mid-write never leaves a half-written file (mirrors
        ``core.data_store.DataStore.save_to_disk``'s ``tmp_file.replace``
        pattern).
        """
        try:
            positions = await self._snapshot_positions()
            orders = await self._snapshot_orders()
            kill_switch_active = self._probe_kill_switch()
            paper_balance = self._probe_paper_balance()
            feature_flags = self._snapshot_feature_flags()

            state: dict[str, Any] = {
                "timestamp": time.time(),
                "schema_version": 1,
                "positions": positions,
                "orders": orders,
                "kill_switch_active": kill_switch_active,
                "paper_balance": paper_balance,
                "feature_flags": feature_flags,
            }

            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(state, f, default=str, indent=2)
            tmp_file.replace(self._state_path)
            logger.debug("[state_recovery] State checkpointed to %s", self._state_path)
        except Exception as e:  # noqa: BLE001 — checkpoint must never break the bot
            logger.error("[state_recovery] Failed to checkpoint state: %s", e)

    def get_last_report(self) -> Optional[RecoveryReport]:
        """Return the most recent ``RecoveryReport`` (or ``None`` if
        ``recover()`` has not yet run).

        Used by the ``GET /api/system/recovery-report`` endpoint so it can
        distinguish "no recovery yet" (``None``) from a legitimate empty
        recovery (fresh boot — non-``None`` report with zeros).
        """
        return self._last_report

    # ── Internals ─────────────────────────────────────────────────────────

    def _load_state(self) -> Optional[dict[str, Any]]:
        """Load the persisted recovery state from disk.

        Returns ``None`` if the file is absent or cannot be parsed — the
        caller (``recover``) treats both as a fresh-boot signal so the bot
        always starts even if the prior checkpoint was corrupt.
        """
        try:
            if self._state_path.exists():
                with open(self._state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
                logger.warning(
                    "[state_recovery] State file %s is not a JSON object — ignoring",
                    self._state_path,
                )
        except json.JSONDecodeError as e:
            logger.error(
                "[state_recovery] Failed to parse state file %s: %s",
                self._state_path,
                e,
            )
        except OSError as e:
            logger.error(
                "[state_recovery] Failed to read state file %s: %s",
                self._state_path,
                e,
            )
        return None

    def _find_stale_orders(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Find orders that were open at shutdown — may have filled during downtime.

        An order is "stale" iff its ``status`` field is in
        ``STALE_ORDER_STATUSES`` (the non-terminal OSM / data_store states).
        Terminal orders (``FILLED`` / ``CANCELLED`` / ``REJECTED`` /
        ``EXPIRED``) are NOT stale — their lifecycle is closed and the
        bot's accounting already reflects them.
        """
        stale: list[dict[str, Any]] = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            status = str(order.get("status", "")).upper()
            if status in STALE_ORDER_STATUSES:
                stale.append(order)
        return stale

    # ── Live-subsystem probes (used by both ``recover`` and ``checkpoint``)

    def _probe_kill_switch(self) -> bool:
        """Return ``True`` if the durable kill-switch marker file exists.

        Lazy import + try/except so a broken ``core.safety`` import (rare —
        same package — but the pattern is consistent with how every other
        cross-subsystem probe in ``core.data_store._broadcast_*`` is
        guarded) never breaks the recovery path.
        """
        try:
            from core.safety import kill_switch_file_exists

            return bool(kill_switch_file_exists())
        except Exception as e:  # noqa: BLE001 — probe must never break recovery
            logger.debug("[state_recovery] kill_switch probe failed: %s", e)
            return False

    def _probe_paper_balance(self) -> float:
        """Return the live ``store.paper_balance`` (or ``BANKROLL_BASELINE``
        if the store hasn't been initialised yet).
        """
        try:
            from core.data_store import BANKROLL_BASELINE, store

            return float(getattr(store, "paper_balance", BANKROLL_BASELINE))
        except Exception as e:  # noqa: BLE001
            logger.debug("[state_recovery] paper_balance probe failed: %s", e)
            return 100.0

    def _probe_flag_count(self) -> int:
        """Return the number of feature flags in the live cache."""
        try:
            from core.feature_flags import flag_manager

            return len(flag_manager.get_all())
        except Exception as e:  # noqa: BLE001
            logger.debug("[state_recovery] flag_manager probe failed: %s", e)
            return 0

    async def _snapshot_positions(self) -> list[dict[str, Any]]:
        """Return the live open positions as JSON-serialisable dicts.

        Awaits ``store.get_positions()`` (the portfolio-optimizer contract)
        so the snapshot reflects the post-fill state. Empty when no
        positions are open.
        """
        try:
            from core.data_store import store

            positions = await store.get_positions()
            # Defensive — ``store.get_positions`` always returns a list, but
            # a future refactor or a mock might return None.
            return list(positions) if positions else []
        except Exception as e:  # noqa: BLE001 — snapshot must never break checkpoint
            logger.debug("[state_recovery] positions snapshot failed: %s", e)
            return []

    async def _snapshot_orders(self) -> list[dict[str, Any]]:
        """Return the live open orders as JSON-serialisable dicts.

        Awaits ``store.get_open_orders()`` (returns a list of ``Order``
        dataclass instances) and projects each to a JSON-safe dict so the
        next restart's ``recover()`` can classify it as stale or terminal.
        """
        try:
            from core.data_store import store

            open_orders = await store.get_open_orders()
            snapshot: list[dict[str, Any]] = []
            for order in open_orders:
                snapshot.append(
                    {
                        "order_id": _to_str(getattr(order, "order_id", "")),
                        "token_id": _to_str(getattr(order, "token_id", "")),
                        "side": _to_str(getattr(order, "side", "")),
                        "price": float(getattr(order, "price", 0.0)),
                        "size": float(getattr(order, "size", 0.0)),
                        "size_matched": float(getattr(order, "size_matched", 0.0)),
                        "status": _to_str(getattr(order, "status", "")),
                        "strategy": _to_str(getattr(order, "strategy", "")),
                        "paper": bool(getattr(order, "paper", False)),
                        "created_at": float(getattr(order, "created_at", 0.0)),
                        "decision_id": _to_str(getattr(order, "decision_id", "")),
                    }
                )
            return snapshot
        except Exception as e:  # noqa: BLE001 — snapshot must never break checkpoint
            logger.debug("[state_recovery] orders snapshot failed: %s", e)
            return []

    def _snapshot_feature_flags(self) -> dict[str, Any]:
        """Return the live feature-flag cache as a ``{key: enabled}`` dict.

        Compact shape (key → bool) rather than the full ``FeatureFlag``
        dataclass because the checkpoint file is a recovery snapshot, not
        an audit trail — only the current on/off state matters for
        "what was the bot's posture at shutdown?".
        """
        try:
            from core.feature_flags import flag_manager

            return {f["key"]: bool(f["enabled"]) for f in flag_manager.get_all()}
        except Exception as e:  # noqa: BLE001 — snapshot must never break checkpoint
            logger.debug("[state_recovery] feature flags snapshot failed: %s", e)
            return {}


# ── Module-level singleton ──────────────────────────────────────────────────
# Production callers do ``from core.state_recovery import state_recovery`` then
# ``await state_recovery.recover()``. Tests construct a fresh
# ``StateRecoveryManager(tmp_path / "recovery_state.json")`` against a
# ``tmp_path`` JSON file for full isolation.
state_recovery = StateRecoveryManager()


def report_to_dict(report: Optional[RecoveryReport]) -> dict[str, Any]:
    """Serialise a ``RecoveryReport`` (or ``None``) to a JSON-safe dict.

    Helper for the ``GET /api/system/recovery-report`` endpoint so the
    route handler is a one-liner. Returns ``{"status":
    "no_recovery_yet"}`` when ``report is None`` so a client polling the
    endpoint before the lifespan startup phase ran gets a clear signal
    rather than an empty 200.
    """
    if report is None:
        return {"status": "no_recovery_yet"}
    return asdict(report)


__all__ = [
    "RECOVERY_STATE_PATH",
    "STALE_ORDER_STATUSES",
    "RecoveryReport",
    "StateRecoveryManager",
    "state_recovery",
    "report_to_dict",
]
