#!/usr/bin/env python3
"""Ingestion backfill runner — CLI tool for triggering data backfills.

W33-5 — wires the :class:`ingestion.backfill.BackfillEngine`,
:mod:`ingestion.pipeline`, :mod:`ingestion.raw_vault`, and
:mod:`ingestion.dead_letter` modules to a single operator CLI so an
operator can:

  * run a backfill pass (markets / prices / trades / outcomes / all)
    against the live ingestion store
  * print the live ingestion-pipeline + health-monitor snapshot
  * replay raw records from the durable raw-vault
  * list / retry the dead-letter queue

Usage
~~~~~

.. code-block:: bash

    python scripts/run_ingestion.py backfill --type markets
    python scripts/run_ingestion.py backfill --type prices --token <token_id> --days 30
    python scripts/run_ingestion.py backfill --type trades --days 7
    python scripts/run_ingestion.py backfill --type outcomes --days 90
    python scripts/run_ingestion.py backfill --type all --days 30
    python scripts/run_ingestion.py status
    python scripts/run_ingestion.py replay --source clob --from <timestamp>
    python scripts/run_ingestion.py dlq --list
    python scripts/run_ingestion.py dlq --retry

Subcommands
~~~~~~~~~~~

  * ``backfill`` — invoke the backfill engine. ``--type`` selects the
    pass (``markets`` maps to ``BackfillType.METADATA`` internally;
    ``all`` runs every pass in sequence). ``--token`` restricts
    prices / trades / snapshots to a single market. ``--days`` sets
    the historical depth (prices / trades only; ignored for
    metadata / outcomes).
  * ``status`` — print the live pipeline counters (running flag,
    active-source count, total events, failed records) and the
    cross-source health summary (events received / failed / error
    rate / throughput / avg latency / DLQ depth / alerts).
  * ``replay`` — fetch raw records from the raw-vault by source and
    optional start timestamp. Prints the count + the first few
    record ids (does NOT re-process them — operators wire the
    output to a downstream consumer manually).
  * ``dlq`` — manage the dead-letter queue. ``--list`` prints the
    most recent 50 pending records; ``--retry`` marks every pending
    record as retried (success=True) so the queue drains.

Exit codes
~~~~~~~~~~

  * ``0`` — subcommand completed successfully.
  * ``1`` — subcommand crashed (e.g. the backfill engine raised an
    unhandled exception, or the DLQ retry failed at the storage
    layer).
  * ``2`` — invalid CLI flags (argparse's default).

Implementation notes
~~~~~~~~~~~~~~~~~~~~

  * The script mirrors the env-var bootstrap pattern in
    ``scripts/run_backfill.py`` (W31-3) — every on-disk
    persisted-state path is redirected to ``/tmp/pmbot_cli`` before
    any project module is imported so an operator can run the CLI
    without first exporting a half-dozen env vars.
  * The template's ``BackfillPipeline`` reference is intentionally
    NOT a separate class — :class:`BackfillEngine` already exposes
    the same surface via ``engine.run(type, market_token=, days=,
    resume=)``. Adding a parallel pipeline wrapper would just be
    boilerplate with no behaviour gain. The CLI's ``backfill``
    subcommand adapts the template's ``--type markets`` /
    ``--token`` / ``--days`` flags into ``BackfillEngine.run``'s
    keyword contract.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# ── Defensive env redirects BEFORE any project import ───────────────────────
# Mirrors the bootstrap pattern in ``scripts/run_backfill.py`` (W31-3).
# The repo's ``core/timescale_db`` and the sibling ``ingestion`` package
# (``raw_vault``, ``dead_letter``, ``checkpoint``, ``health`` …) read
# ``MARKET_DB_PATH`` / ``BOT_DATA_DIR`` / ``DLQ_DB_PATH`` etc. at module-
# import time and would otherwise try to mkdir ``/app/data`` (read-only in
# the sandbox). Redirecting them to ``/tmp/pmbot_cli`` lets an operator
# run ``python scripts/run_ingestion.py`` without exporting any env vars.
_TMP_ROOT = Path(os.environ.get("PMBOT_CLI_TMP_ROOT", "/tmp/pmbot_cli"))
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
for _key, _default in (
    ("MARKET_DB_PATH", str(_TMP_ROOT / "market_intelligence.db")),
    ("BOT_DATA_DIR", str(_TMP_ROOT / "dao_data")),
    ("DLQ_DB_PATH", str(_TMP_ROOT / "dead_letter.db")),
    ("RAW_VAULT_DB_PATH", str(_TMP_ROOT / "raw_vault.db")),
    ("CHECKPOINT_DB_PATH", str(_TMP_ROOT / "checkpoints.db")),
    ("LINEAGE_DB_PATH", str(_TMP_ROOT / "lineage.db")),
    ("ALERT_DB_PATH", str(_TMP_ROOT / "alerts.db")),
    ("TRADING_MODE", "paper"),
    ("LIVE_TRADING_ENABLED", "false"),
):
    os.environ.setdefault(_key, _default)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*`` / ``ingestion.*``) regardless of the cwd the CLI was
# launched from — mirrors the bootstrap pattern in
# ``scripts/run_backfill.py`` and ``tests/conftest.py``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Argument parser ─────────────────────────────────────────────────────────


# Mapping from the CLI's user-facing ``--type`` value (kept stable so
# the docstring usage examples don't drift) to the engine's internal
# ``BackfillType`` value. ``markets`` maps to ``metadata`` because the
# backfill engine's metadata pass IS the "all markets" pass — see
# ``ingestion/backfill.py`` docstring §1 for the rationale.
BACKFILL_TYPE_ALIASES: dict[str, str] = {
    "markets": "metadata",
    "prices": "prices",
    "trades": "trades",
    "outcomes": "outcomes",
    "all": "all",
}

VALID_BACKFILL_TYPES = tuple(BACKFILL_TYPE_ALIASES.keys())


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the argparse namespace for the CLI.

    Exposed as a module-level function (rather than inline in ``main``)
    so the test suite can call it directly without invoking
    ``sys.exit`` (mirrors the pattern in ``scripts/run_backfill.py``'s
    ``_parse_args`` helper).
    """
    parser = argparse.ArgumentParser(
        prog="run_ingestion.py",
        description="Ingestion management CLI — backfill, status, replay, DLQ.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Backfill
    bf = subparsers.add_parser(
        "backfill",
        help="Run a data backfill pass (markets / prices / trades / outcomes / all).",
    )
    bf.add_argument(
        "--type",
        choices=VALID_BACKFILL_TYPES,
        required=True,
        help="Backfill type to run. 'markets' = metadata, 'all' = "
             "metadata + prices + trades + outcomes + snapshots.",
    )
    bf.add_argument(
        "--token",
        default=None,
        help="Restrict to a single token_id (prices / trades only).",
    )
    bf.add_argument(
        "--days",
        type=int,
        default=30,
        help="Historical depth in days (default: 30). Prices + trades only.",
    )
    bf.add_argument(
        "--no-resume",
        action="store_true",
        help="Reset the checkpoint and start from offset 0 / first token.",
    )
    bf.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )

    # Status
    subparsers.add_parser(
        "status",
        help="Show ingestion pipeline + health-monitor snapshot.",
    )

    # Replay
    rp = subparsers.add_parser(
        "replay",
        help="Replay raw records from the durable raw-vault.",
    )
    rp.add_argument(
        "--source",
        required=True,
        help="Source identifier (e.g. 'clob_rest', 'gamma_api', 'ws_book').",
    )
    rp.add_argument(
        "--from",
        dest="from_ts",
        type=float,
        default=None,
        help="Start timestamp (Unix seconds). Records older than this are skipped.",
    )
    rp.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Maximum records to fetch (default: 1000).",
    )

    # Dead-letter queue
    dlq = subparsers.add_parser(
        "dlq",
        help="Manage the dead-letter queue (list / retry).",
    )
    dlq.add_argument(
        "--list",
        action="store_true",
        help="Print the 50 most recent pending DLQ records.",
    )
    dlq.add_argument(
        "--retry",
        action="store_true",
        help="Mark every pending DLQ record as retried (drains the queue).",
    )
    dlq.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Number of records to print for --list (default: 50).",
    )

    args = parser.parse_args(argv)

    if args.command == "dlq" and not (args.list or args.retry):
        parser.error("dlq requires --list or --retry")
    return args


# ── Subcommand handlers ──────────────────────────────────────────────────────


async def run_backfill(args: argparse.Namespace) -> int:
    """Run a backfill pass against the :class:`BackfillEngine`.

    Translates the CLI's ``--type markets`` / ``--token`` / ``--days``
    flags into the engine's ``engine.run(type, market_token=, days=,
    resume=)`` contract. The engine returns a dict mapping each
    executed backfill type to its :class:`BackfillStats`; this handler
    pretty-prints one summary line per type and returns 0 on success.
    """
    from ingestion.backfill import BackfillEngine  # noqa: E402  (sys.path first)

    engine = BackfillEngine()
    engine_type = BACKFILL_TYPE_ALIASES[args.type]

    try:
        results = await engine.run(
            engine_type,
            market_token=args.token,
            days=args.days,
            resume=not args.no_resume,
        )
    except Exception as e:
        print(f"Backfill failed: {e}", file=sys.stderr)
        return 1

    print("\n=== Backfill summary ===")
    for name, stats in results.items():
        d = stats.to_dict()
        print(
            f"  type={d['type']:<10} "
            f"added={d['total_added']:>5} "
            f"skipped={d['total_skipped']:>5} "
            f"errors={d['total_errors']:>5} "
            f"elapsed={d['elapsed_s']:.2f}s"
        )
        if d["total_errors"]:
            print(f"    (last error: {d['error_message'] or '<see logs>'})")
    print(f"\nBackfill complete: {len(results)} pass(es).")
    return 0


async def show_status() -> int:
    """Print the live ingestion pipeline + health-monitor snapshot.

    Surfaces four pipeline counters (running / active sources / total
    events / failed records) and the full health-monitor summary dict
    (events received + failed + error rate + throughput + avg latency
    + DLQ depth + alerts). Mirrors the JSON shape that
    ``GET /api/status`` already returns so an operator can use the
    same mental model for both surfaces.
    """
    from ingestion.health import ingestion_health_monitor  # noqa: E402
    from ingestion.pipeline import ingestion_pipeline  # noqa: E402

    status = {
        "running": ingestion_pipeline.is_running,
        "active_sources": ingestion_pipeline.active_sources,
        "total_events": ingestion_pipeline.total_events,
        "failed_count": ingestion_pipeline.failed_count,
    }
    health = ingestion_health_monitor.get_summary()

    print("Ingestion Pipeline Status:")
    print(f"  Running: {status['running']}")
    print(f"  Active sources: {status['active_sources']}")
    print(f"  Events ingested: {status['total_events']}")
    print(f"  Failed records: {status['failed_count']}")
    print("\nHealth Summary:")
    for k, v in health.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.4f}")
        else:
            print(f"  {k}: {v}")
    return 0


async def run_replay(args: argparse.Namespace) -> int:
    """Replay raw records from the durable raw-vault.

    Uses :meth:`RawVault.replay_range` (NOT ``RawVault.replay`` — the
    latter takes a single ``observation_id`` and is the per-record
    fetch path, not the bulk-replay path). The CLI prints the count
    plus a per-record summary line; it does NOT re-process the records
    (operators wire the output to a downstream consumer manually).
    """
    from ingestion.raw_vault import raw_vault  # noqa: E402

    records = list(
        raw_vault.replay_range(
            start_ts=args.from_ts,
            source=args.source,
            limit=args.limit,
        )
    )
    print(f"Replaying {len(records)} records from {args.source!r}")
    for r in records[:20]:
        obs_id = r.get("observation_id", "")
        event_ts = r.get("event_timestamp", 0.0)
        print(f"  {obs_id} (event_ts={event_ts})")
    if len(records) > 20:
        print(f"  ... ({len(records) - 20} more)")
    return 0


async def manage_dlq(args: argparse.Namespace) -> int:
    """Manage the dead-letter queue (``--list`` / ``--retry``).

    ``--list`` prints the ``--limit`` most recent pending records
    (default 50). ``--retry`` iterates every pending record and marks
    it retried with ``success=True`` so the queue drains — the
    :meth:`DeadLetterQueue.mark_retried` call is the same one the
    API's ``POST /api/ingestion/dead-letter/retry`` endpoint uses, so
    the CLI and the API share the same retry semantics.

    The retry path returns ``1`` if every record's retry failed (e.g.
    the underlying SQLite store is unwritable); otherwise ``0``.
    """
    from ingestion.dead_letter import dead_letter_queue  # noqa: E402

    if args.list:
        items = dead_letter_queue.get_pending(limit=args.limit)
        print(f"Dead-letter queue ({len(items)} pending items):")
        for item in items:
            print(
                f"  {item.record_id}  source={item.source}  "
                f"type={item.record_type}  reason={item.reason}  "
                f"retries={item.retry_count}  status={item.status}"
            )
            if item.error:
                err = item.error[:120]
                print(f"    error: {err}")
        return 0

    if args.retry:
        items = dead_letter_queue.get_pending(limit=10_000)
        if not items:
            print("Dead-letter queue is empty — nothing to retry.")
            return 0
        succeeded = 0
        for item in items:
            ok = dead_letter_queue.mark_retried(item.record_id, success=True)
            if ok:
                succeeded += 1
        print(
            f"Retried {succeeded} / {len(items)} records "
            f"({len(items) - succeeded} failed to mark retried)."
        )
        return 0 if succeeded == len(items) else 1

    # Unreachable — ``_parse_args`` rejects ``dlq`` without --list / --retry.
    print("dlq requires --list or --retry", file=sys.stderr)
    return 1


# ── Dispatch ────────────────────────────────────────────────────────────────


async def _dispatch(args: argparse.Namespace) -> int:
    """Route a parsed namespace to the matching async handler.

    Returns the handler's exit code (or ``0`` for ``--help`` / no
    subcommand — argparse's ``--help`` exits before this function is
    reached, but if the operator invokes the CLI with no subcommand at
    all, ``args.command`` is ``None`` and we fall through to the
    argparse help printout).
    """
    if args.command == "backfill":
        return await run_backfill(args)
    if args.command == "status":
        return await show_status()
    if args.command == "replay":
        return await run_replay(args)
    if args.command == "dlq":
        return await manage_dlq(args)
    # No subcommand — argparse's subparser machinery already prints a
    # help line; we exit 0 so an interactive shell prompt doesn't see
    # a misleading non-zero code from a bare ``run_ingestion.py``.
    print("Use --help for usage.", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point — parses argv, configures logging, dispatches."""
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        return asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 0
    except Exception as e:  # noqa: BLE001 — top-level CLI must never crash
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
