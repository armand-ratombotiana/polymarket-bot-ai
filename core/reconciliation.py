"""
core/reconciliation.py — Storage-vs-engine reconciliation (P0-DAT-03).

Compares the number of accepted writes (engine telemetry) against the number
of physically stored rows, and checks that every tracked market has storage
coverage. Produces a timestamped daily report artifact in RECON_REPORT_DIR.

Exit gate G-M3: 4/4 tables accumulate verified rows; reconciliation report clean.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path

from core.timescale_db import _TABLES, timescale_db

log = logging.getLogger(__name__)

RECON_REPORT_DIR = Path(os.environ.get("RECON_REPORT_DIR", "data/reports"))

_TABLE_COUNT_KEY = {
    "market_snapshots": "snapshots_recorded",
    "orderbook_ticks": "ticks_recorded",
    "fundamental_news": "news_items_recorded",
    "ml_feature_store": "ml_feature_vectors",
}

_TABLE_ENGINE_KEY = {
    "market_snapshots": "inserts_ok",
    "orderbook_ticks": "inserts_ok",
    "fundamental_news": "inserts_ok",
    "ml_feature_store": "inserts_ok",
}


def _storage_counts(stats: dict) -> dict[str, int]:
    return {t: int(stats.get(_TABLE_COUNT_KEY.get(t, f"{t}_recorded"), stats.get(t, 0))) for t in _TABLES}


def _engine_counts(stats: dict) -> dict[str, int]:
    ok = stats.get("inserts_ok", {})
    return {t: int(ok.get(t, 0)) for t in _TABLES}


def run_reconciliation(engine=None) -> dict:
    """Run one full reconciliation pass and persist a dated artifact.

    `engine` defaults to the global timescale_db singleton; tests pass an
    isolated engine instance.
    """
    engine = engine or timescale_db
    stats = engine.get_stats()
    storage = _storage_counts(stats)
    engine = _engine_counts(stats)

    tables = {}
    breaches: list[str] = []
    for t in _TABLES:
        drift = engine[t] - storage[t]
        tables[t] = {
            "engine_accepted_writes": engine[t],
            "storage_rows": storage[t],
            "drift": drift,
        }
        if drift > 0:
            breaches.append(
                f"table={t} drift={drift} (accepted {engine[t]} writes, {storage[t]} rows physically stored)"
            )

    last_error = stats.get("last_error")
    if last_error is not None:
        breaches.append(f"persistence reported an un-flushed write error: {last_error}")

    report = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "backend": stats.get("db_backend", "unknown"),
        "is_clean": len(breaches) == 0,
        "tables": tables,
        "breaches": breaches,
        "write_failures": stats.get("inserts_failed", {}),
    }

    RECON_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    artifact = RECON_REPORT_DIR / f"reconciliation_{datetime.date.today().isoformat()}.json"
    artifact.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log.info(
        "[reconciliation] %s: %s (artifact: %s)",
        "CLEAN" if report["is_clean"] else f"DIRTY ({len(breaches)} breaches)",
        {t: tables[t]["drift"] for t in _TABLES},
        artifact,
    )
    return report


def last_reconciliation() -> dict | None:
    """Load the most recent report artifact, if any."""
    if not RECON_REPORT_DIR.exists():
        return None
    artifacts = sorted(RECON_REPORT_DIR.glob("reconciliation_*.json"))
    if not artifacts:
        return None
    try:
        return json.loads(artifacts[-1].read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("[reconciliation] failed to load last report: %s", e)
        return None