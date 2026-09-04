"""Data quality monitor — checks for stale, missing, or anomalous data.

Runs periodic checks against the canonical ``market_snapshots`` table
(created by ``core/market_db.py`` / ``core/timescale_db.py`` and written to
by ``core/book_poller.py`` via ``core/timescale_db.record_snapshot``):

  ┌────────────────┬───────────────────────────────────────────────────────┐
  │ category       │ example check                                         │
  ├────────────────┼───────────────────────────────────────────────────────┤
  │ freshness      │ How old is the latest market snapshot?               │
  │ completeness   │ Are there enough tracked markets? Any NULL mid prices?│
  │ validity       │ Negative prices? Prices > 1.0 (out-of-probability)?  │
  │ consistency    │ (reserved for future cross-field checks)              │
  │ anomaly        │ (reserved for future pattern checks)                  │
  └────────────────┴───────────────────────────────────────────────────────┘

The HTTP layer (``api/server.py``) appends ``GET /api/data-quality`` at the
end of file, importing the ``data_quality_monitor`` singleton from this
module and calling ``run_all_checks()`` on each request. The endpoint is
read-only — no mutations, no caching — so an operator polling it cannot
perturb the trading pipeline.

Design notes
------------
* **No import-time DB init** — unlike ``core/observability.py`` /
  ``core/decision_ledger.py``, this module does NOT construct the SQLite
  schema at import time. It only reads (best-effort) from whatever DB
  ``MARKET_DB_PATH`` points at, so a missing / unwritable ``/app/data``
  in the sandbox is harmless. Every check is wrapped in ``try/except``
  so a schema drift / missing table surfaces as a single ``fail``
  ``QualityCheck`` instead of an unhandled exception.
* **Singleton ``data_quality_monitor``** mirrors the
  ``observability`` / ``decision_ledger`` convention so importers can grab
  it at module import time.
* **Status derivation** — ``critical`` (≥1 fail) > ``degraded`` (≥1
  warning, 0 fails) > ``healthy`` (everything passes). Empty / missing
  DB is treated as ``critical`` because the freshness check explicitly
  fails when no rows exist.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class QualityCheck:
    """A single data-quality check result."""

    name: str
    category: str  # freshness, completeness, validity, consistency, anomaly
    status: str  # "pass", "warning", "fail"
    value: Any
    threshold: Any
    message: str
    timestamp: float


@dataclass
class DataQualityReport:
    """Aggregate report across every check, with a derived overall status."""

    overall_status: str  # "healthy", "degraded", "critical"
    checks: list[QualityCheck]
    summary: dict[str, Any]
    timestamp: float


class DataQualityMonitor:
    """Monitors data quality across all data sources.

    Currently scoped to the ``market_snapshots`` SQLite table (the
    canonical price/spread/mid store written by ``core/book_poller.py``
    via ``core/timescale_db.record_snapshot``). Future checks can extend
    ``run_all_checks`` to cover additional tables / sources.
    """

    def __init__(self, db_path: str | None = None) -> None:
        # Default to ``/app/data/market_intelligence.db`` (the canonical
        # path used by ``core/market_db.py`` and ``core/timescale_db.py``)
        # rather than ``/app/data/market.db`` — the latter does not exist
        # anywhere in the repo and would always return "fail" in
        # production. ``MARKET_DB_PATH`` env var (set by conftest in
        # tests) overrides both defaults.
        self._db_path = (
            db_path
            or os.environ.get(
                "MARKET_DB_PATH", "/app/data/market_intelligence.db"
            )
        )

    def run_all_checks(self) -> DataQualityReport:
        """Run all data quality checks and return a structured report.

        Each check category (``_check_freshness`` / ``_check_completeness``
        / ``_check_validity``) is independently wrapped in ``try/except``
        so a failure in one category does NOT short-circuit the others.
        The overall status is derived from the worst individual check:

          * any ``fail`` → ``critical``
          * else any ``warning`` → ``degraded``
          * else → ``healthy``
        """
        checks: list[QualityCheck] = []

        # Freshness checks (also emits the tracked-markets completeness check).
        checks.extend(self._check_freshness())

        # Completeness checks.
        checks.extend(self._check_completeness())

        # Validity checks.
        checks.extend(self._check_validity())

        fails = sum(1 for c in checks if c.status == "fail")
        warnings = sum(1 for c in checks if c.status == "warning")

        if fails > 0:
            overall = "critical"
        elif warnings > 0:
            overall = "degraded"
        else:
            overall = "healthy"

        return DataQualityReport(
            overall_status=overall,
            checks=checks,
            summary={
                "total_checks": len(checks),
                "passed": sum(1 for c in checks if c.status == "pass"),
                "warnings": warnings,
                "failed": fails,
            },
            timestamp=time.time(),
        )

    # ── Freshness ──────────────────────────────────────────────────────────

    def _check_freshness(self) -> list[QualityCheck]:
        """Check how fresh the data is.

        Two checks live here:

        1. ``market_data_freshness`` — age of the latest ``timestamp`` in
           ``market_snapshots`` (``fail`` if > 60s old).
        2. ``tracked_markets_count`` — distinct ``token_id`` values with
           a snapshot in the last 5 minutes (``warning`` if < 10).

        The second check is conceptually a *completeness* check but is
        emitted from this method because it shares the same DB connection
        and same broad "is the pipeline feeding us fresh data?" question
        as the age check — bundling them avoids a second connection
        open / close cycle on every ``run_all_checks`` call.
        """
        checks: list[QualityCheck] = []

        try:
            with sqlite3.connect(self._db_path) as conn:
                # Check latest snapshot timestamp.
                row = conn.execute(
                    "SELECT MAX(timestamp) as latest FROM market_snapshots"
                ).fetchone()

                latest = row[0] if row and row[0] else 0
                age = time.time() - latest
                threshold = 60  # 60 seconds

                status = "pass" if age < threshold else "fail"
                checks.append(
                    QualityCheck(
                        name="market_data_freshness",
                        category="freshness",
                        status=status,
                        value=f"{age:.1f}s old",
                        threshold=f"< {threshold}s",
                        message=f"Latest market data is {age:.1f} seconds old",
                        timestamp=time.time(),
                    )
                )

                # Check number of tracked markets (snapshots in last 5 min).
                row = conn.execute(
                    "SELECT COUNT(DISTINCT token_id) as count "
                    "FROM market_snapshots WHERE timestamp > ?",
                    (time.time() - 300,),
                ).fetchone()
                market_count = row[0] if row else 0

                status = "pass" if market_count >= 10 else "warning"
                checks.append(
                    QualityCheck(
                        name="tracked_markets_count",
                        category="completeness",
                        status=status,
                        value=market_count,
                        threshold=">= 10",
                        message=(
                            f"{market_count} markets with data in last 5 minutes"
                        ),
                        timestamp=time.time(),
                    )
                )
        except Exception as e:
            # Defensive: a missing table / unwritable path / locked DB
            # surfaces as a single ``fail`` check so the report's overall
            # status is ``critical`` instead of crashing the caller.
            checks.append(
                QualityCheck(
                    name="market_data_freshness",
                    category="freshness",
                    status="fail",
                    value="error",
                    threshold="N/A",
                    message=f"Failed to check: {e}",
                    timestamp=time.time(),
                )
            )

        return checks

    # ── Completeness ───────────────────────────────────────────────────────

    def _check_completeness(self) -> list[QualityCheck]:
        """Check for missing fields.

        Currently checks for NULL / zero ``mid`` prices — a snapshot row
        whose ``mid`` is missing can't drive the strategy / ML layers, so
        even a single occurrence is worth flagging as a warning.
        """
        checks: list[QualityCheck] = []

        try:
            with sqlite3.connect(self._db_path) as conn:
                # Check for null / zero mid prices.
                row = conn.execute(
                    "SELECT COUNT(*) FROM market_snapshots "
                    "WHERE mid IS NULL OR mid = 0"
                ).fetchone()
                null_count = row[0] if row else 0

                status = "pass" if null_count == 0 else "warning"
                checks.append(
                    QualityCheck(
                        name="null_mid_prices",
                        category="completeness",
                        status=status,
                        value=null_count,
                        threshold="0",
                        message=f"{null_count} snapshots with null/zero mid price",
                        timestamp=time.time(),
                    )
                )
        except Exception as e:
            # If the table is missing the freshness check already
            # surfaced a ``fail``; here we just log + return empty so
            # the overall status derivation isn't double-counting the
            # same root cause.
            logger.warning("Completeness check failed: %s", e)

        return checks

    # ── Validity ──────────────────────────────────────────────────────────

    def _check_validity(self) -> list[QualityCheck]:
        """Check for invalid values.

        Two checks:

        1. ``negative_prices`` — ``best_bid`` or ``best_ask`` < 0
           (impossible for a probability market — ``fail`` if any).
        2. ``prices_over_1`` — ``best_bid`` or ``best_ask`` > 1.0
           (out-of-probability range — ``warning`` because some
           multi-outcome markets can technically have ``> 1`` quoted
           prices on a single token before normalisation).
        """
        checks: list[QualityCheck] = []

        try:
            with sqlite3.connect(self._db_path) as conn:
                # Check for negative prices.
                row = conn.execute(
                    "SELECT COUNT(*) FROM market_snapshots "
                    "WHERE best_bid < 0 OR best_ask < 0"
                ).fetchone()
                neg_count = row[0] if row else 0

                status = "pass" if neg_count == 0 else "fail"
                checks.append(
                    QualityCheck(
                        name="negative_prices",
                        category="validity",
                        status=status,
                        value=neg_count,
                        threshold="0",
                        message=f"{neg_count} snapshots with negative prices",
                        timestamp=time.time(),
                    )
                )

                # Check for prices > 1.0 (should be 0-1 for probability markets).
                row = conn.execute(
                    "SELECT COUNT(*) FROM market_snapshots "
                    "WHERE best_bid > 1.0 OR best_ask > 1.0"
                ).fetchone()
                over_count = row[0] if row else 0

                status = "pass" if over_count == 0 else "warning"
                checks.append(
                    QualityCheck(
                        name="prices_over_1",
                        category="validity",
                        status=status,
                        value=over_count,
                        threshold="0",
                        message=f"{over_count} snapshots with prices > 1.0",
                        timestamp=time.time(),
                    )
                )
        except Exception as e:
            logger.warning("Validity check failed: %s", e)

        return checks


# Module-level singleton — mirrors the ``observability`` / ``decision_ledger``
# convention so importers can grab it at module import time. No DB init is
# performed at construction, so importing this module in a sandbox without
# a writable ``/app/data`` is safe.
data_quality_monitor = DataQualityMonitor()


__all__ = [
    "QualityCheck",
    "DataQualityReport",
    "DataQualityMonitor",
    "data_quality_monitor",
]
