"""
core/retention.py — Data Retention Pruning.

Bounded-storage policy enforcement for the four SQLite ledgers backing the
trading pipeline. Each ledger lives in its own DB file (env-var-driven, same
convention as ``core/observability.py`` / ``core/decision_ledger.py`` /
``core/execution_quality.py`` / ``core/audit_logger.py``) so pruning one store
never perturbs another's schema or immutability contract.

Retention windows (mirrors the S13/S14/S15 module conventions):

  ┌────────────────────────────┬───────────┬────────────────────────────────────────┐
  │ store                      │ retention │ env var                                │
  ├────────────────────────────┼───────────┼────────────────────────────────────────┤
  │ observability (metrics)    │ 7 days    │ OBSERVABILITY_DB_PATH                  │
  │ decision_ledger            │ 30 days   │ DECISION_LEDGER_DB_PATH                │
  │   (decision_events +       │           │                                        │
  │    decision_rejections)    │           │                                        │
  │ execution_quality          │ 30 days   │ EXECUTION_QUALITY_DB_PATH              │
  │ audit_events               │ 90 days   │ AUDIT_DB_PATH                          │
  └────────────────────────────┴───────────┴────────────────────────────────────────┘

The observability store rolls fastest (high-frequency system snapshots → 7
days), the audit trail rolls slowest (forensic / compliance window → 90 days),
and the decision-ledger / execution-quality stores sit in between at 30 days
(long enough to reconstruct a trade's full lifecycle for any recent decision,
short enough to keep the SQLite files compact for the dashboard queries).

The HTTP layer (``api/server.py``) calls ``register_routes(app)`` at startup to
expose:

  POST /api/system/prune        invoke a single store prune or run_all_pruning()

Design notes
------------
- **Sync I/O**: every prune function is synchronous (SQLite ``DELETE`` is fast
  on the bounded rowcounts these tables reach in practice). The HTTP endpoint
  wraps ``run_all_pruning`` / single-store calls in ``asyncio.to_thread`` so
  the FastAPI event loop is never blocked.
- **Defensive**: every public function swallows its own persistence errors
  (logged at ``error`` level, returns 0 / partial result) — a retention hiccup
  can never break the trading pipeline. This mirrors the
  ``decision_ledger.record`` / ``observability.record_metric`` contract.
- **SQL-injection safe**: ``prune_old_data(table, ...)`` validates the table
  name against ``^[a-zA-Z_][a-zA-Z0-9_]*$`` and raises ``ValueError`` on
  mismatch — table names cannot be parameterised in SQLite, so a strict
  identifier check is the only safe pattern. The four specialised prune
  functions hard-code their table names so they bypass the regex path is a
  no-op (still validated, but never a programmer footgun).
- **Idempotent / safe on every boot**: ``DELETE`` against a non-existent table
  or DB file is caught + logged + returns 0, so a cron / scheduler can call
  ``run_all_pruning()`` on a fresh boot with empty stores without raising.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── DB paths (env-var-driven; mirror the sibling modules' defaults) ─────────
OBSERVABILITY_DB_PATH = Path(
    os.environ.get("OBSERVABILITY_DB_PATH", "/app/data/observability.db")
)
DECISION_LEDGER_DB_PATH = Path(
    os.environ.get("DECISION_LEDGER_DB_PATH", "/app/data/decision_ledger.db")
)
EXECUTION_QUALITY_DB_PATH = Path(
    os.environ.get("EXECUTION_QUALITY_DB_PATH", "/app/data/execution_quality.db")
)
AUDIT_DB_PATH = Path(os.environ.get("AUDIT_DB_PATH", "/app/data/audit_trail.db"))

# ── Retention windows (hours) ───────────────────────────────────────────────
# Centralised here so a future operator can tune the policy in one place.
OBSERVABILITY_RETENTION_HOURS = 7 * 24       # 7 days — high-frequency metrics
DECISION_LEDGER_RETENTION_HOURS = 30 * 24    # 30 days — full decision lifecycle
EXECUTION_QUALITY_RETENTION_HOURS = 30 * 24  # 30 days — per-fill slippage stats
AUDIT_EVENTS_RETENTION_HOURS = 90 * 24       # 90 days — forensic / compliance

_HOURS_TO_SECONDS = 3600.0

# Strict identifier regex — table names can't be parameterised in SQLite, so
# any caller-supplied table name must match this pattern before substitution.
# Conservative: bare ASCII letters, digits, underscore; must start with a
# letter or underscore (matches every table name in the four sibling modules).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ── Generic primitive ───────────────────────────────────────────────────────

def prune_old_data(
    table: str,
    max_age_hours: float,
    db_path: str | Path | None = None,
) -> int:
    """
    Delete rows older than ``max_age_hours`` from ``table`` in ``db_path``.

    Args:
        table: target SQLite table name. Must match ``^[A-Za-z_][A-Za-z0-9_]*$``
            — SQLite cannot parameterise identifiers, so a strict regex
            check is enforced before substitution. Raises ``ValueError`` on
            any other shape (programmer error, not runtime data).
        max_age_hours: rows whose ``timestamp`` column is older than
            ``now - max_age_hours * 3600`` are deleted. Must be ≥ 0; a
            value of 0 deletes every row (use with care).
        db_path: SQLite file path. If ``None``, the call is a logged no-op
            returning 0 (lets the specialised functions skip silently when
            an env var is unset).

    Returns:
        The number of rows actually deleted (``cursor.rowcount``). 0 if the
        table is empty, the DB file is absent, or any persistence error
        occurred (the error is logged at ``error`` level, never raised).

    The ``timestamp`` column is assumed to be a ``REAL`` epoch-seconds value
    — every table created by ``core/observability.py``,
    ``core/decision_ledger.py``, ``core/execution_quality.py``, and
    ``core/audit_logger.py`` honours this contract.
    """
    if not isinstance(table, str) or not _IDENTIFIER_RE.match(table):
        # Programmer error — surface loudly so callers can't accidentally
        # construct a SQL-injection vector via string concatenation.
        raise ValueError(
            f"prune_old_data: invalid table name {table!r} "
            "(must match ^[A-Za-z_][A-Za-z0-9_]*$)"
        )
    try:
        max_age_hours = float(max_age_hours)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"prune_old_data: max_age_hours must be numeric, got {max_age_hours!r}"
        ) from e
    if max_age_hours < 0:
        raise ValueError(
            f"prune_old_data: max_age_hours must be >= 0, got {max_age_hours}"
        )

    if db_path is None:
        log.debug(
            "[retention] prune_old_data table=%s skipped — no db_path", table,
        )
        return 0

    db_path = Path(db_path)
    if not db_path.exists():
        # Fresh boot / never-yet-initialised store — nothing to prune.
        log.debug(
            "[retention] prune_old_data table=%s skipped — db not found: %s",
            table, db_path,
        )
        return 0

    cutoff = time.time() - (max_age_hours * _HOURS_TO_SECONDS)
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            # Table name is regex-validated above — safe to interpolate.
            # The cutoff is parameterised (the only user-influenced value).
            cursor.execute(
                f"DELETE FROM {table} WHERE timestamp < ?",
                (cutoff,),
            )
            deleted = cursor.rowcount if cursor.rowcount is not None else 0
            conn.commit()
        log.info(
            "[retention] prune_old_data table=%s db=%s max_age_hours=%.2f "
            "deleted=%d",
            table, db_path, max_age_hours, deleted,
        )
        return int(deleted)
    except sqlite3.Error as e:
        # Most likely: table doesn't exist in this DB (fresh / foreign file).
        # Caught + logged so a misconfigured env var never breaks the caller.
        log.error(
            "[retention] prune_old_data failed table=%s db=%s: %s",
            table, db_path, e,
        )
        return 0
    except Exception as e:
        # Belt-and-braces: any unexpected failure (permission, disk full,
        # corrupt page, ...) is logged and swallowed so the trading pipeline
        # never crashes on a maintenance operation.
        log.error(
            "[retention] prune_old_data unexpected error table=%s db=%s: %s",
            table, db_path, e,
        )
        return 0


# ── Per-store specialised prunes ────────────────────────────────────────────

def prune_observability(max_age_hours: float = OBSERVABILITY_RETENTION_HOURS) -> int:
    """
    Prune the observability ``metrics`` table (default 7 days).

    High-frequency system snapshots (CPU / memory every ~10 s, per-cycle
    bot metrics, per-signal strategy metrics) → the metrics table grows
    fastest of the four stores, so it carries the shortest retention window.
    """
    return prune_old_data("metrics", max_age_hours, OBSERVABILITY_DB_PATH)


def prune_decision_ledger(
    max_age_hours: float = DECISION_LEDGER_RETENTION_HOURS,
) -> int:
    """
    Prune the decision-ledger tables (default 30 days).

    The ledger DB carries two tables — the ordered stage chain
    (``decision_events``) and the fast-filtered rejection view
    (``decision_rejections``). Both are pruned against the same cutoff so a
    token's PREDICTION → SIGNAL → RISK_* → ORDER → FILL chain stays
    internally consistent (no orphan events left on the main chain after
    their rejection row is pruned, and vice versa).

    Returns the **total** rows deleted across both tables.
    """
    n_events = prune_old_data(
        "decision_events", max_age_hours, DECISION_LEDGER_DB_PATH,
    )
    n_rej = prune_old_data(
        "decision_rejections", max_age_hours, DECISION_LEDGER_DB_PATH,
    )
    return n_events + n_rej


def prune_execution_quality(
    max_age_hours: float = EXECUTION_QUALITY_RETENTION_HOURS,
) -> int:
    """
    Prune the ``execution_quality`` table (default 30 days).

    Per-fill slippage / latency / realized-edge rows are kept for a full
    month so the dashboard's rolling 30-day execution-quality view stays
    intact across restarts; anything older is summarised via
    ``get_execution_stats`` aggregates before being dropped.
    """
    return prune_old_data(
        "execution_quality", max_age_hours, EXECUTION_QUALITY_DB_PATH,
    )


def prune_audit_events(
    max_age_hours: float = AUDIT_EVENTS_RETENTION_HOURS,
) -> int:
    """
    Prune the ``audit_events`` table (default 90 days).

    ``core/audit_logger.py`` describes the audit trail as immutable; the
    90-day retention balances that immutability contract against bounded
    storage growth — three months is the typical forensic / compliance
    reconstruction window for a paper-trading pipeline. Live deployments
    that need a longer window should override ``AUDIT_EVENTS_RETENTION_HOURS``
    via env-var / monkeypatch before calling ``run_all_pruning``.
    """
    return prune_old_data("audit_events", max_age_hours, AUDIT_DB_PATH)


# ── Orchestrator ────────────────────────────────────────────────────────────

# Single source of truth for the four targets — used by the route handler to
# map a string target → callable without eval / getattr footguns.
_PRUNE_TARGETS: dict[str, Any] = {
    "observability": prune_observability,
    "decision_ledger": prune_decision_ledger,
    "execution_quality": prune_execution_quality,
    "audit_events": prune_audit_events,
}


def run_all_pruning() -> dict[str, Any]:
    """
    Run every specialised prune in sequence and return a structured summary.

    Returns::

        {
          "timestamp": float,            # epoch seconds at start of run
          "results": {
            "observability": {
              "pruned": int, "max_age_hours": float,
              "db_path": str, "error": str | None,
            },
            "decision_ledger":     { ... },
            "execution_quality":   { ... },
            "audit_events":        { ... },
          },
          "total_pruned": int,
          "success": bool,               # True iff every target had error == None
        }

    Each target's function is invoked in its own ``try/except`` — a single
    failure does not abort the rest of the run. The ``error`` field is
    ``None`` on success or a stringified exception on failure (the prune
    functions themselves are already defensive and return 0 on error, but
    this wrapper catches any unexpected raise too).
    """
    started = time.time()
    results: dict[str, Any] = {}
    total = 0
    all_ok = True

    # (target_name, callable, default_max_age_hours, db_path_for_summary)
    targets: list[tuple[str, Any, float, Path]] = [
        ("observability",     prune_observability,     OBSERVABILITY_RETENTION_HOURS,    OBSERVABILITY_DB_PATH),
        ("decision_ledger",   prune_decision_ledger,  DECISION_LEDGER_RETENTION_HOURS,  DECISION_LEDGER_DB_PATH),
        ("execution_quality", prune_execution_quality, EXECUTION_QUALITY_RETENTION_HOURS, EXECUTION_QUALITY_DB_PATH),
        ("audit_events",      prune_audit_events,      AUDIT_EVENTS_RETENTION_HOURS,    AUDIT_DB_PATH),
    ]

    for name, fn, default_age, db_path in targets:
        try:
            pruned = int(fn())
            err: str | None = None
        except Exception as e:
            # Defensive — the individual prunes already swallow errors, but
            # this guards against any future regression that lets one raise.
            log.error("[retention] run_all_pruning: %s raised: %s", name, e)
            pruned = 0
            err = f"{type(e).__name__}: {e}"
            all_ok = False
        results[name] = {
            "pruned": pruned,
            "max_age_hours": float(default_age),
            "db_path": str(db_path),
            "error": err,
        }
        total += pruned

    summary: dict[str, Any] = {
        "timestamp": started,
        "results": results,
        "total_pruned": total,
        "success": all_ok,
    }
    log.info(
        "[retention] run_all_pruning complete: total_pruned=%d success=%s",
        total, all_ok,
    )
    return summary


# ── FastAPI route registration ──────────────────────────────────────────────

# ── FastAPI request model (module-level so FastAPI resolves it as a body) ───
# Mirrors the convention in ``api/server.py`` (every Pydantic request model is
# defined at module scope, e.g. ``ObservationModeRequest``) and the pattern in
# ``core/live_safety_gate.py`` (``EnableLiveRequest``). Defining the model
# inside ``register_routes`` combined with ``from __future__ import
# annotations`` made FastAPI treat the parameter as a query arg (``loc:
# ["query","req"]``) instead of a JSON body — the annotation string
# ``"_PruneRequest | None"`` couldn't be resolved against the function's
# local namespace. Module scope makes it resolvable via ``get_type_hints()`.
try:
    from pydantic import BaseModel, Field

    class PruneRequest(BaseModel):
        """Request body for ``POST /api/system/prune``.

        Body is optional — omitting it (or sending ``{}``) defaults
        ``target`` to ``"all"`` so a plain ``POST /api/system/prune`` with
        no body runs every prune.
        """

        target: str = Field(
            default="all",
            description=(
                "Which store to prune: 'all' (default), 'observability', "
                "'decision_ledger', 'execution_quality', or 'audit_events'."
            ),
        )
except Exception:  # pragma: no cover — pydantic is a hard project dep; this only trips in odd unit-test stubs
    PruneRequest = None  # type: ignore[assignment, misc]


def register_routes(app: Any) -> None:
    """
    Append the data-retention endpoint to a FastAPI app.

    Endpoints (auth-protected by the caller's existing middleware):

      POST /api/system/prune
          Trigger a data-retention prune across one or all stores.

          Request body (JSON, optional — defaults to ``{"target": "all"}``):

              {"target": "all" | "observability" | "decision_ledger" |
                          "execution_quality" | "audit_events"}

          - ``target=all`` (default): runs ``run_all_pruning()`` and returns
            the full structured summary (per-store pruned counts, total, db
            paths, per-store errors).
          - Any other target: runs just that one prune function and returns
            ``{"target": <name>, "pruned": <int>}``.

          Returns 400 on an unknown target. Never 500s on a DB error — the
          underlying prune functions swallow + log persistence errors and
          report ``pruned=0`` (or ``error`` set in the all-target summary).
    """
    # Local imports — FastAPI is optional at module load (same convention as
    # ``core/observability.py`` / ``core/decision_ledger.py``). Pydantic is
    # imported eagerly at module top so the request model is resolvable from
    # the route handler's annotation.
    from fastapi import HTTPException

    @app.post("/api/system/prune", tags=["system"])
    async def _system_prune(req: "PruneRequest | None" = None):
        """Trigger a data-retention prune across one or all stores."""
        target = ((req.target if req else None) or "all").strip().lower()
        if target == "all":
            return await asyncio.to_thread(run_all_pruning)
        fn = _PRUNE_TARGETS.get(target)
        if fn is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown prune target: {target!r}. "
                    f"Valid targets: all, {', '.join(_PRUNE_TARGETS)}."
                ),
            )
        pruned = await asyncio.to_thread(fn)
        return {"target": target, "pruned": int(pruned)}


__all__ = [
    # DB path constants (env-var-driven — mirror sibling modules)
    "OBSERVABILITY_DB_PATH",
    "DECISION_LEDGER_DB_PATH",
    "EXECUTION_QUALITY_DB_PATH",
    "AUDIT_DB_PATH",
    # Retention windows (hours)
    "OBSERVABILITY_RETENTION_HOURS",
    "DECISION_LEDGER_RETENTION_HOURS",
    "EXECUTION_QUALITY_RETENTION_HOURS",
    "AUDIT_EVENTS_RETENTION_HOURS",
    # Prune primitive + specialised functions
    "prune_old_data",
    "prune_observability",
    "prune_decision_ledger",
    "prune_execution_quality",
    "prune_audit_events",
    "run_all_pruning",
    # FastAPI route registration
    "PruneRequest",
    "register_routes",
]
