#!/usr/bin/env python3
"""``scripts/run_backfill.py`` — CLI runner for the historical backfill pipeline.

W31-3 — wires the :class:`ingestion.backfill.BackfillEngine` to a CLI
so an operator can rebuild any subset of the historical data store
without touching the live bot.

Usage
~~~~~

Run from the polymarket-bot project root (or any directory)::

    python scripts/run_backfill.py --type metadata
    python scripts/run_backfill.py --type prices --days 30 --resolution 1h
    python scripts/run_backfill.py --type trades --market <token_id> --days 7
    python scripts/run_backfill.py --type outcomes
    python scripts/run_backfill.py --type snapshots --market <token_id>
    python scripts/run_backfill.py --type all --days 14
    python scripts/run_backfill.py --type metadata --no-resume
    python scripts/run_backfill.py --list-markets

Flags
~~~~~

  * ``--type`` / ``-t`` — backfill type: ``metadata`` / ``prices`` /
    ``trades`` / ``outcomes`` / ``snapshots`` / ``all``. Required (unless
    ``--list-markets`` is given).
  * ``--market`` / ``-m`` — restrict the backfill to a single market
    (``condition_id`` for metadata / outcomes, ``token_id`` for prices
    / trades / snapshots). Optional.
  * ``--days`` / ``-d`` — historical depth in days (default 7; prices
    + trades only).
  * ``--resolution`` / ``-r`` — OHLCV candle resolution for the price
    backfill (``1m`` / ``5m`` / ``15m`` / ``1h`` / ``1d``; default ``1h``).
  * ``--no-resume`` — start the backfill from offset 0 / first token
    rather than picking up from the persisted checkpoint.
  * ``--concurrency`` / ``-c`` — parallel fetch workers for token-level
    fan-out (default 4).
  * ``--rps`` — target requests-per-second for the shared rate limiter
    (default 5.0).
  * ``--list-markets`` — print every market currently in the
    ``backfill_markets`` table and exit (no backfill performed).
  * ``--list-runs`` — print the most recent N ``backfill_runs`` rows
    and exit. Defaults to N=10.

Exit codes
~~~~~~~~~~

  * ``0`` — backfill completed (with or without per-item errors; the
    per-item errors are reported in the stats summary but do not fail
    the run).
  * ``1`` — backfill crashed at the orchestration level (e.g. an
    unhandled exception inside the engine's ``run`` method).
  * ``2`` — invalid CLI flags (e.g. unknown ``--type``).
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
# The repo's ``core/timescale_db`` and the parallel-agent modules in the
# ``ingestion`` package (``raw_vault``, ``dead_letter``, ``checkpoint``,
# ``health`` …) read ``MARKET_DB_PATH`` / ``BOT_DATA_DIR`` / etc. at
# module-import time and would otherwise try to mkdir ``/app/data`` (which
# is read-only in this sandbox). The conftest in ``tests/conftest.py``
# already does this for the test suite; we mirror the pattern here for
# the CLI so an operator can run ``python scripts/run_backfill.py``
# without first exporting a half-dozen env vars.
_TMP_ROOT = Path(os.environ.get("PMBOT_CLI_TMP_ROOT", "/tmp/pmbot_cli"))
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
for _key, _default in (
    ("MARKET_DB_PATH", str(_TMP_ROOT / "market_intelligence.db")),
    ("BOT_DATA_DIR", str(_TMP_ROOT / "dao_data")),
    ("TRADING_MODE", "paper"),
    ("LIVE_TRADING_ENABLED", "false"),
):
    os.environ.setdefault(_key, _default)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``ingestion.*``) regardless of the cwd the CLI was
# launched from — mirrors the bootstrap pattern in
# ``scripts/migrate_db.py`` and ``tests/conftest.py``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.backfill import (  # noqa: E402  (sys.path first)
    DEFAULT_CONCURRENCY,
    DEFAULT_DAYS,
    DEFAULT_PAGE_SIZE,
    DEFAULT_MAX_PAGES,
    DEFAULT_PRICE_RESOLUTION,
    DEFAULT_RATE_LIMIT_RPS,
    BackfillEngine,
    BackfillStore,
    BackfillType,
)

VALID_TYPES = (
    "metadata", "prices", "trades", "outcomes", "snapshots", "all",
)
VALID_RESOLUTIONS = ("1m", "5m", "15m", "1h", "1d")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_backfill.py",
        description="Run a historical backfill pass against Gamma / CLOB.",
    )
    p.add_argument(
        "-t", "--type",
        choices=VALID_TYPES,
        help="Backfill type to run (default: metadata).",
    )
    p.add_argument(
        "-m", "--market",
        default=None,
        help="Restrict to a single market (condition_id / token_id).",
    )
    p.add_argument(
        "-d", "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Historical depth in days (default: {DEFAULT_DAYS}). "
             f"Prices + trades only.",
    )
    p.add_argument(
        "-r", "--resolution",
        choices=VALID_RESOLUTIONS,
        default=DEFAULT_PRICE_RESOLUTION,
        help=f"OHLCV candle resolution (default: {DEFAULT_PRICE_RESOLUTION}). "
             f"Prices only.",
    )
    p.add_argument(
        "--no-resume",
        action="store_true",
        help="Reset the checkpoint and start from offset 0 / first token.",
    )
    p.add_argument(
        "-c", "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Parallel fetch workers for token fan-out (default: {DEFAULT_CONCURRENCY}).",
    )
    p.add_argument(
        "--rps",
        type=float,
        default=DEFAULT_RATE_LIMIT_RPS,
        help=f"Target requests-per-second (default: {DEFAULT_RATE_LIMIT_RPS}).",
    )
    p.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"Gamma API page size (default: {DEFAULT_PAGE_SIZE}).",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"Safety cap on pagination depth (default: {DEFAULT_MAX_PAGES}).",
    )
    p.add_argument(
        "--list-markets",
        action="store_true",
        help="Print every market in the backfill_markets table and exit.",
    )
    p.add_argument(
        "--list-runs",
        type=int,
        default=0,
        nargs="?",
        const=10,
        help="Print the N most recent backfill_runs rows and exit "
             "(default N=10 if flag is given without a value).",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    args = p.parse_args(argv)

    if not args.type and not args.list_markets and not args.list_runs:
        p.error("one of --type / --list-markets / --list-runs is required")
    if args.type and args.type not in VALID_TYPES:
        p.error(f"--type must be one of {VALID_TYPES}")
    return args


def _format_stats(stats_dict: dict[str, Any]) -> str:
    """Pretty-print a single :class:`BackfillStats` dict."""
    return (
        f"  type={stats_dict['type']:<10} "
        f"added={stats_dict['total_added']:>5} "
        f"skipped={stats_dict['total_skipped']:>5} "
        f"errors={stats_dict['total_errors']:>5} "
        f"elapsed={stats_dict['elapsed_s']:.2f}s"
    )


async def _run_backfill(args: argparse.Namespace) -> int:
    engine = BackfillEngine(
        target_rps=args.rps,
        concurrency=args.concurrency,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )
    results = await engine.run(
        args.type,
        market_token=args.market,
        days=args.days,
        resolution=args.resolution,
        resume=not args.no_resume,
    )
    print("\n=== Backfill summary ===")
    for name, stats in results.items():
        print(_format_stats(stats.to_dict()))
        if stats.total_errors:
            print(f"    (last error: {stats.error_message or '<see logs>'})")
    return 0


def _list_markets() -> int:
    store = BackfillStore()
    markets = store.list_markets(limit=500)
    if not markets:
        print("(no markets in backfill_markets — run --type metadata first)")
        return 0
    print(f"=== {len(markets)} markets (showing first 500) ===")
    print(f"{'condition_id':<66} {'slug':<30} {'active':<6} {'closed':<6} {'resolved':<8}")
    for m in markets:
        resolved = (
            "YES" if m.get("resolved_outcome_yes") == 1
            else "NO" if m.get("resolved_outcome_yes") == 0
            else "-"
        )
        print(
            f"{(m.get('condition_id') or ''):<66} "
            f"{(m.get('slug') or '')[:30]:<30} "
            f"{int(bool(m.get('active'))):<6} "
            f"{int(bool(m.get('closed'))):<6} "
            f"{resolved:<8}"
        )
    return 0


def _list_runs(n: int) -> int:
    store = BackfillStore()
    try:
        import sqlite3
        with sqlite3.connect(store._sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM backfill_runs ORDER BY id DESC LIMIT ?",
                (int(n),),
            )
            rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"Failed to read backfill_runs: {e}")
        return 1
    if not rows:
        print("(no backfill runs yet)")
        return 0
    print(f"=== {len(rows)} most recent backfill_runs ===")
    for r in rows:
        started = r.get("started_at") or 0.0
        ended = r.get("ended_at") or 0.0
        elapsed = max(0.0, ended - started)
        print(
            f"  #{r['id']:<4} type={r['type']:<10} "
            f"added={r['total_added']:>5} "
            f"skipped={r['total_skipped']:>5} "
            f"errors={r['total_errors']:>5} "
            f"elapsed={elapsed:.2f}s"
        )
        if r.get("error_message"):
            print(f"         error: {r['error_message']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.list_markets:
        return _list_markets()
    if args.list_runs:
        return _list_runs(args.list_runs if isinstance(args.list_runs, int) else 10)

    try:
        return asyncio.run(_run_backfill(args))
    except KeyboardInterrupt:
        print("\nInterrupted — partial progress was checkpointed.")
        return 0
    except Exception as e:
        print(f"Backfill failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
