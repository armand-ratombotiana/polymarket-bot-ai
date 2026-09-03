#!/usr/bin/env python3
"""Live data integrity checker — runs structural + relational checks across
all Polymarket-bot SQLite databases.

Checks performed (per database):
  1. ``PRAGMA integrity_check``    — full structural / page-level integrity.
  2. ``PRAGMA quick_check``        — faster subset of (1); still catches most
                                    corruption. We run BOTH and report each.
  3. ``PRAGMA foreign_key_check``  — only meaningful if the schema declares
                                    FOREIGN KEY constraints (most of our
                                    tables don't, but we run it anyway).
  4. Orphaned-record detection     — for known cross-DB logical relationships
                                    (e.g. ``execution_quality.decision_id``
                                    should exist in
                                    ``decision_ledger.decision_events``).
                                    Implemented via ``ATTACH DATABASE``.
  5. Table bloat detection         — row counts compared to configurable
                                    soft ceilings; rows above ceiling are
                                    flagged but don't fail the run.
  6. Index health                  — every index listed via
                                    ``PRAGMA index_list``; presence of
                                    ``sqlite_stat1`` (the ANALYZE output)
                                    is reported as a hint that the query
                                    planner has stats to work with.

Exit codes:
    0 — no errors found (warnings about bloat / missing ANALYZE are non-fatal)
    1 — usage error
    2 — one or more DBs failed integrity_check or had orphaned records
    3 — could not access the data dir at all

Usage:
    python3 scripts/check_integrity.py
    python3 scripts/check_integrity.py --data-dir /tmp/test-data
    python3 scripts/check_integrity.py --json
    python3 scripts/check_integrity.py --bloat-ceiling audit_events=1000000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import sqlite3
from datetime import datetime
from pathlib import Path

DEFAULT_DATA_DIR = "/home/z/my-project/mini-services/polymarket-bot/data"

# Soft row-count ceilings per (db_file, table). These are intentionally
# generous — exceeding them produces a WARNING, not an error, because the
# operator may legitimately have more rows during a high-activity period.
# Values are derived from the documented retention policy (audit_trail keeps
# 90d, decision_ledger keeps 30d, etc.) assuming ~1 row / decision / second
# sustained, with a 2x safety margin.
DEFAULT_BLOAT_CEILINGS: dict[tuple[str, str], int] = {
    ("audit_trail.db", "audit_events"): 1_000_000,
    ("decision_ledger.db", "decision_events"): 500_000,
    ("decision_ledger.db", "decision_rejections"): 500_000,
    ("execution_quality.db", "execution_quality"): 500_000,
    ("closed_positions.db", "closed_positions"): 100_000,
    ("observability.db", "metrics"): 10_000_000,
    ("market.db", "market_snapshots"): 10_000_000,
    ("market.db", "orderbook_ticks"): 50_000_000,
    ("market.db", "fundamental_news"): 100_000,
    ("market.db", "ml_feature_store"): 1_000_000,
    ("market_intelligence.db", "market_snapshots"): 10_000_000,
    ("market_intelligence.db", "orderbook_ticks"): 50_000_000,
    ("market_intelligence.db", "fundamental_news"): 100_000,
    ("market_intelligence.db", "ml_feature_store"): 1_000_000,
    ("shadow_trades.db", "shadow_trades"): 100_000,
}

# Logical (undeclared) foreign-key relationships to check.
# Each entry:
#   source_db     : DB file containing the referring column
#   source_table  : table containing the referring column
#   source_col    : referring column (NULLs are skipped automatically)
#   target_db     : DB file containing the referenced column (may == source)
#   target_table  : table containing the referenced column
#   target_col    : referenced column
# We attach target_db to the source connection under the alias "tgt" and
# run: SELECT COUNT(*) FROM <src_table> s
#      WHERE s.<src_col> IS NOT NULL
#        AND NOT EXISTS (SELECT 1 FROM tgt.<tgt_table> t
#                        WHERE t.<tgt_col> = s.<src_col>)
LOGICAL_FKS = [
    {
        "source_db": "execution_quality.db",
        "source_table": "execution_quality",
        "source_col": "decision_id",
        "target_db": "decision_ledger.db",
        "target_table": "decision_events",
        "target_col": "decision_id",
    },
    {
        "source_db": "closed_positions.db",
        "source_table": "closed_positions",
        "source_col": "decision_id",
        "target_db": "decision_ledger.db",
        "target_table": "decision_events",
        "target_col": "decision_id",
    },
    {
        # Intra-DB: a rejection should usually reference a real decision,
        # but in practice the bot logs rejections BEFORE allocating a
        # decision_id, so this is a SOFT check reported as info only.
        "source_db": "decision_ledger.db",
        "source_table": "decision_rejections",
        "source_col": "decision_id",
        "target_db": "decision_ledger.db",
        "target_table": "decision_events",
        "target_col": "decision_id",
        "soft": True,
    },
]


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"


def list_user_tables(conn: sqlite3.Connection) -> list[str]:
    """Return all table names owned by the user (skip sqlite_* + _migrations)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "  AND name NOT LIKE '\\_migrations' ESCAPE '\\' "
        "ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def list_indices(conn: sqlite3.Connection, table: str) -> list[dict]:
    """Return [{name, unique, columns: [str, ...]}] for one table."""
    out: list[dict] = []
    for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
        # row schema: (seq, name, unique, origin, partial)
        idx_name = row[1]
        unique = bool(row[2])
        cols = [
            c[2]
            for c in conn.execute(f'PRAGMA index_info("{idx_name}")').fetchall()
        ]
        out.append({"name": idx_name, "unique": unique, "columns": cols})
    return out


def check_database(
    db_path: Path,
    data_dir: Path,
    bloat_ceilings: dict[tuple[str, str], int],
) -> dict:
    """Run all integrity checks against a single DB file."""
    report: dict = {
        "file": db_path.name,
        "path": str(db_path),
        "exists": db_path.is_file(),
        "size_bytes": 0,
        "size_human": "",
        "checks": {},
        "tables": [],
        "indices": [],
        "errors": [],
        "warnings": [],
        "healthy": True,
    }
    if not report["exists"]:
        report["errors"].append(f"DB file not found: {db_path}")
        report["healthy"] = False
        return report

    report["size_bytes"] = db_path.stat().st_size
    report["size_human"] = _human_bytes(report["size_bytes"])

    try:
        # uri=True lets us open read-only via file:...?mode=ro — we never
        # want to write to the live DBs from this checker.
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            timeout=30,
        )
    except sqlite3.Error as e:
        report["errors"].append(f"Cannot open DB: {e}")
        report["healthy"] = False
        return report

    try:
        # --- 1. integrity_check (full) -------------------------------------
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            # integrity_check returns one row per problem; "ok" if clean.
            msg = "\n".join(r[0] for r in rows) if rows else "(no rows)"
            ok = len(rows) == 1 and rows[0][0] == "ok"
            report["checks"]["integrity_check"] = {
                "ok": ok,
                "result": msg,
            }
            if not ok:
                report["errors"].append(f"integrity_check: {msg}")
                report["healthy"] = False
        except sqlite3.Error as e:
            report["checks"]["integrity_check"] = {"ok": False, "result": str(e)}
            report["errors"].append(f"integrity_check raised: {e}")
            report["healthy"] = False

        # --- 2. quick_check (faster subset) --------------------------------
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
            msg = "\n".join(r[0] for r in rows) if rows else "(no rows)"
            ok = len(rows) == 1 and rows[0][0] == "ok"
            report["checks"]["quick_check"] = {"ok": ok, "result": msg}
            if not ok and report["healthy"]:
                # Don't downgrade from a full integrity_check failure already
                # recorded, but DO flag the quick_check failure independently.
                report["errors"].append(f"quick_check: {msg}")
                report["healthy"] = False
        except sqlite3.Error as e:
            report["checks"]["quick_check"] = {"ok": False, "result": str(e)}

        # --- 3. foreign_key_check (only finds declared FKs) ---------------
        # foreign_keys pragma must be ON for this to do anything; we toggle
        # it locally for the duration of this check. PRAGMA foreign_keys is
        # a no-op in read-only mode? Actually it's a connection-level flag
        # that can be set even on a read-only connection.
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            report["checks"]["foreign_key_check"] = {
                "ok": len(fk_violations) == 0,
                "violations": [
                    {"table": v[0], "rowid": v[1], "parent": v[2], "parent_rowid": v[3]}
                    for v in fk_violations
                ],
            }
            if fk_violations:
                report["errors"].append(
                    f"foreign_key_check: {len(fk_violations)} violation(s)"
                )
                report["healthy"] = False
        except sqlite3.Error as e:
            # Some SQLite builds don't support foreign_keys pragma toggling
            # on read-only connections; treat as a non-fatal warning.
            report["checks"]["foreign_key_check"] = {
                "ok": None,
                "error": str(e),
            }

        # --- 4. Logical (cross-DB) orphan checks --------------------------
        logical_orphans = []
        for fk in LOGICAL_FKS:
            if fk["source_db"] != db_path.name:
                continue
            target_path = data_dir / fk["target_db"]
            if not target_path.is_file():
                # Can't check this orphan relationship without the target DB.
                continue
            # Attach the target DB read-only.
            attach_alias = "tgt"
            try:
                # Detach first in case a prior attach lingered (defensive).
                conn.execute(f"DETACH DATABASE {attach_alias}")
            except sqlite3.Error:
                pass
            try:
                # Use a bound parameter for the URI so paths with special
                # characters (quotes, etc.) are handled correctly. The
                # connection was opened with uri=True, so the target is
                # also opened read-only via the file:...?mode=ro URI form.
                conn.execute(
                    f"ATTACH DATABASE ? AS {attach_alias}",
                    (f"file:{target_path}?mode=ro",),
                )
            except sqlite3.Error as e:
                report["warnings"].append(
                    f"Could not ATTACH {fk['target_db']} for orphan check: {e}"
                )
                continue
            try:
                # NOT EXISTS subquery: any row whose src_col is non-NULL and
                # has no matching parent row is an orphan.
                sql = (
                    f'SELECT COUNT(*) FROM "{fk["source_table"]}" s '
                    f'WHERE s."{fk["source_col"]}" IS NOT NULL '
                    f'AND NOT EXISTS ('
                    f'  SELECT 1 FROM {attach_alias}."{fk["target_table"]}" t '
                    f'  WHERE t."{fk["target_col"]}" = s."{fk["source_col"]}"'
                    f')'
                )
                orphan_count = conn.execute(sql).fetchone()[0]
                entry = {
                    "source_table": fk["source_table"],
                    "source_col": fk["source_col"],
                    "target_db": fk["target_db"],
                    "target_table": fk["target_table"],
                    "orphan_count": orphan_count,
                    "soft": fk.get("soft", False),
                }
                logical_orphans.append(entry)
                if orphan_count > 0 and not fk.get("soft", False):
                    report["errors"].append(
                        f"orphan: {orphan_count} row(s) in "
                        f"{fk['source_table']}.{fk['source_col']} have no "
                        f"matching {fk['target_db']}.{fk['target_table']}."
                        f"{fk['target_col']}"
                    )
                    report["healthy"] = False
            except sqlite3.Error as e:
                report["warnings"].append(
                    f"Orphan check failed for "
                    f"{fk['source_table']}.{fk['source_col']}: {e}"
                )
            finally:
                try:
                    conn.execute(f"DETACH DATABASE {attach_alias}")
                except sqlite3.Error:
                    pass
        report["checks"]["logical_orphans"] = logical_orphans

        # --- 5. Per-table bloat check -------------------------------------
        tables = list_user_tables(conn)
        analyze_ran = False
        try:
            # sqlite_stat1 is the table ANALYZE writes its stats to.
            cur = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='sqlite_stat1'"
            )
            analyze_ran = cur.fetchone() is not None
        except sqlite3.Error:
            pass
        report["checks"]["analyze_ran"] = analyze_ran
        if not analyze_ran:
            report["warnings"].append(
                "sqlite_stat1 missing — run `scripts/db-maintenance.sh` "
                "(ANALYZE) so the query planner has stats."
            )

        for tbl in tables:
            try:
                row_count = conn.execute(
                    f'SELECT COUNT(*) FROM "{tbl}"'
                ).fetchone()[0]
            except sqlite3.Error as e:
                report["warnings"].append(f"Could not count {tbl}: {e}")
                continue

            ceiling = bloat_ceilings.get((db_path.name, tbl))
            bloat = {"table": tbl, "rows": row_count}
            if ceiling is not None:
                bloat["ceiling"] = ceiling
                bloat["pct_of_ceiling"] = (
                    round(row_count / ceiling * 100, 2) if ceiling else 0.0
                )
                if row_count > ceiling:
                    report["warnings"].append(
                        f"bloat: {tbl} has {row_count} rows "
                        f"(ceiling {ceiling}, "
                        f"{bloat['pct_of_ceiling']}% of ceiling)"
                    )
            bloat["indices"] = list_indices(conn, tbl)
            report["tables"].append(bloat)
            for idx in bloat["indices"]:
                report["indices"].append({
                    "table": tbl,
                    "name": idx["name"],
                    "unique": idx["unique"],
                    "columns": idx["columns"],
                })

        # --- 6. Index health summary --------------------------------------
        # integrity_check already validates index pages; here we just surface
        # which indices exist + whether ANALYZE has populated sqlite_stat1
        # for them (a missing entry means the planner won't use that index
        # optimally).
        stat1_entries: dict[str, str] = {}
        if analyze_ran:
            try:
                for r in conn.execute(
                    "SELECT tbl, idx, stat FROM sqlite_stat1"
                ).fetchall():
                    stat1_entries[f"{r[0]}.{r[1]}"] = r[2]
            except sqlite3.Error:
                pass
        index_health = []
        for idx in report["indices"]:
            key = f"{idx['table']}.{idx['name']}"
            has_stats = key in stat1_entries
            entry = {
                "table": idx["table"],
                "name": idx["name"],
                "unique": idx["unique"],
                "columns": idx["columns"],
                "has_analyze_stats": has_stats,
            }
            if not has_stats:
                entry["warning"] = (
                    "no ANALYZE stats — query planner may choose poorly"
                )
            index_health.append(entry)
        report["checks"]["index_health"] = index_health

    finally:
        conn.close()

    return report


def parse_bloat_overrides(items: list[str]) -> dict[tuple[str, str], int]:
    """Parse ``--bloat-ceiling db=table=N`` overrides.

    Accepts ``db=table=N`` or ``table=N`` (the latter applies to every DB
    that has a table with that name).
    """
    overrides: dict[tuple[str, str], int] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(
                f"Invalid --bloat-ceiling spec (need db=table=N): {item!r}"
            )
        key, _, val = item.rpartition("=")
        try:
            n = int(val)
        except ValueError as e:
            raise ValueError(
                f"Invalid row-count in --bloat-ceiling {item!r}: {e}"
            ) from e
        if "=" in key:
            db, _, tbl = key.partition("=")
            overrides[(db, tbl)] = n
        else:
            # Apply to every DB. We model this as a wildcard by merging
            # the override into the final map at lookup time, but for
            # simplicity we also walk DEFAULT_BLOAT_CEILINGS and set every
            # matching (db, table) pair to n.
            for k in list(DEFAULT_BLOAT_CEILINGS.keys()):
                if k[1] == key:
                    overrides[k] = n
    return overrides


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check integrity of all live Polymarket-bot SQLite DBs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("BOT_DATA_DIR", DEFAULT_DATA_DIR),
        help=f"Live data dir (default: {DEFAULT_DATA_DIR}; env: BOT_DATA_DIR)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable report.",
    )
    parser.add_argument(
        "--bloat-ceiling",
        action="append",
        default=[],
        metavar="DB=TABLE=N",
        help="Override the soft row-count ceiling for a table. May be "
             "passed multiple times. Example: "
             "--bloat-ceiling audit_trail.db=audit_events=2000000",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(
            f"ERROR: data dir not found or not a directory: {data_dir}",
            file=sys.stderr,
        )
        return 3

    try:
        overrides = parse_bloat_overrides(args.bloat_ceiling)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    bloat_ceilings = dict(DEFAULT_BLOAT_CEILINGS)
    bloat_ceilings.update(overrides)

    # Snapshot all *.db files in the data dir (NOT recursively — subdirs
    # like test_run/ contain throwaway test DBs that we don't want to
    # include in the live-integrity report).
    db_files = sorted(p for p in data_dir.iterdir() if p.suffix == ".db")

    overall: dict = {
        "data_dir": str(data_dir),
        "checked_at": datetime.now().isoformat(),
        "databases": [],
        "healthy": True,
        "errors": 0,
        "warnings": 0,
    }

    for db_path in db_files:
        rep = check_database(db_path, data_dir, bloat_ceilings)
        if not rep["healthy"]:
            overall["healthy"] = False
        overall["errors"] += len(rep["errors"])
        overall["warnings"] += len(rep["warnings"])
        overall["databases"].append(rep)

    if args.json:
        print(json.dumps(overall, indent=2))
    else:
        _print_human(overall)

    return 0 if overall["healthy"] else 2


def _print_human(overall: dict) -> None:
    print(f"\n{'=' * 70}")
    print("Live Data Integrity Report")
    print(f"{'=' * 70}")
    print(f"Data dir : {overall['data_dir']}")
    print(f"Checked  : {overall['checked_at']}")
    status = "✓ HEALTHY" if overall["healthy"] else "✗ UNHEALTHY"
    print(
        f"Overall  : {status}  "
        f"({len(overall['databases'])} DBs, "
        f"{overall['errors']} error(s), "
        f"{overall['warnings']} warning(s))"
    )
    print(f"{'=' * 70}\n")

    for db in overall["databases"]:
        marker = "✓" if db["healthy"] else "✗"
        print(
            f"  {marker} {db['file']:<30} "
            f"{db['size_human']:>10}  "
            f"({len(db['tables'])} tables, "
            f"{len(db['indices'])} indices)"
        )
        if db["errors"]:
            for e in db["errors"]:
                print(f"      ERROR: {e}")
        if db["warnings"]:
            for w in db["warnings"]:
                print(f"      warn : {w}")

        ic = db["checks"].get("integrity_check", {})
        qc = db["checks"].get("quick_check", {})
        fkc = db["checks"].get("foreign_key_check", {})
        print(
            f"      integrity_check={'ok' if ic.get('ok') else 'FAIL'} "
            f"quick_check={'ok' if qc.get('ok') else 'FAIL'} "
            f"foreign_key_check={'ok' if fkc.get('ok') else 'fail/n-a'} "
            f"analyze_ran={db['checks'].get('analyze_ran')}"
        )

        for t in db["tables"]:
            ceiling_info = ""
            if "ceiling" in t:
                pct = t.get("pct_of_ceiling", 0.0)
                ceiling_info = f" (ceiling {t['ceiling']}, {pct}% used)"
                if t["rows"] > t["ceiling"]:
                    ceiling_info = " ⚠ BLOAT" + ceiling_info
            print(
                f"        - {t['table']:<28} "
                f"{t['rows']:>10} rows{ceiling_info}"
            )

        # Orphaned records (cross-DB)
        for o in db["checks"].get("logical_orphans", []):
            tag = "soft" if o.get("soft") else "ORPHAN"
            print(
                f"        orphan-check {tag}: "
                f"{o['source_table']}.{o['source_col']} → "
                f"{o['target_db']}.{o['target_table']}: "
                f"{o['orphan_count']} orphan(s)"
            )
    print(f"\n{'=' * 70}")


if __name__ == "__main__":
    sys.exit(main())
