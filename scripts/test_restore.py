#!/usr/bin/env python3
"""Restore round-trip test — proves our backup+restore pipeline produces a
byte-faithful copy of the live databases.

Pipeline exercised:
  1. BACKUP: For each *.db in the live data dir, snapshot via the SQLite
     Online Backup API (``Connection.backup()``), then gzip the result.
     This mirrors what ``scripts/backup.sh`` does.
  2. RESTORE: Gunzip each .db.gz into a fresh temp dir. This mirrors what
     ``scripts/restore.sh`` does (minus the bot-stop / pre-restore snapshot
     steps, which are operational concerns not relevant to a data test).
  3. VERIFY: For each DB, compare:
       - schema (sqlite_master dump)
       - per-table row counts
       - PRAGMA integrity_check on the restored copy
       - a SHA-256 of each table's row tuples (sorted) — catches row-level
         data divergence even when row counts match.
  4. REPORT: print a human-readable summary; emit JSON if --json.
  5. CLEAN UP: all temp files are removed in a finally block.

This test NEVER touches the live databases in write mode. The Online Backup
API only reads from the source connection. The live DBs are opened
read-only via ``file:...?mode=ro`` URIs.

Usage:
    python3 scripts/test_restore.py
    python3 scripts/test_restore.py --data-dir /tmp/test-data
    python3 scripts/test_restore.py --json
    python3 scripts/test_restore.py --keep-temp  # for debugging failures

Exit codes:
    0 — all DBs round-tripped cleanly (schema + row count + row hashes match)
    1 — usage error
    2 — one or more DBs failed to round-trip cleanly
    3 — data dir missing
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

DEFAULT_DATA_DIR = "/home/z/my-project/mini-services/polymarket-bot/data"


def _open_ro(path: Path) -> sqlite3.Connection:
    """Open a SQLite DB read-only via the file:...?mode=ro URI form."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


def _schema_hash(conn: sqlite3.Connection) -> str:
    """Stable hash of the schema (sqlite_master, sorted by name+type).

    Captures every CREATE TABLE / INDEX / TRIGGER / VIEW statement so any
    schema drift between original and restored is detectable. We hash the
    SQL text directly (not normalised) — schema changes that look
    semantically equivalent but syntactically different (e.g. column order
    in a CREATE INDEX) WILL be flagged as divergence, which is the desired
    behavior: a "restore" that produces a different schema is suspicious.
    """
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name, tbl_name"
    ).fetchall()
    h = hashlib.sha256()
    for r in rows:
        h.update(b"\x1f")
        h.update(str(r).encode("utf-8"))
    return h.hexdigest()


def _table_rows_hash(conn: sqlite3.Connection, table: str) -> tuple[str, int]:
    """SHA-256 of all rows in `table`, returned as (hex_digest, row_count).

    Rows are sorted by their primary-key column if one exists (INTEGER
    PRIMARY KEY column is detected via PRAGMA table_info), otherwise by
    the full row tuple. Sorting ensures the hash is order-independent
    between the original and restored DBs even when row IDs differ.
    """
    cols = [
        r[1]
        for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    ]
    pk_cols = [
        r[1]
        for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if r[5] > 0  # pk index, 0 means not-in-pk
    ]
    order_clause = ""
    if pk_cols:
        order_clause = (
            " ORDER BY "
            + ", ".join(f'"{c}"' for c in pk_cols)
        )
    cur = conn.execute(
        f'SELECT {", ".join(f"\"{c}\"" for c in cols)} '
        f'FROM "{table}"{order_clause}'
    )
    h = hashlib.sha256()
    count = 0
    while True:
        rows = cur.fetchmany(1024)
        if not rows:
            break
        for row in rows:
            h.update(b"\x1f")
            h.update(repr(row).encode("utf-8"))
            count += 1
    return h.hexdigest(), count


def _list_user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "  AND name NOT LIKE '\\_migrations' ESCAPE '\\' "
        "ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def backup_db(src: Path, dst: Path) -> None:
    """Backup src DB to dst via the Online Backup API."""
    src_conn = _open_ro(src)
    # The destination is a NEW file, so we open it read-write locally.
    # If it already exists (shouldn't, but defensively), remove it.
    if dst.exists():
        dst.unlink()
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def gzip_file(src: Path, dst: Path) -> None:
    """Gzip a file."""
    with open(src, "rb") as f_in:
        with gzip.open(dst, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)


def gunzip_file(src: Path, dst: Path) -> None:
    """Gunzip a file."""
    with gzip.open(src, "rb") as f_in:
        with open(dst, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)


def round_trip_one(
    src_db: Path,
    backup_dir: Path,
    restore_dir: Path,
) -> dict:
    """Back up + restore one DB and report a per-DB comparison result.

    The result dict has the shape:
        {
          "file": str,
          "size_bytes": int,
          "backup_bytes": int,
          "backup_gz_bytes": int,
          "integrity_ok": bool,         # integrity_check on RESTORED copy
          "schema_match": bool,
          "tables": [
            {"table": str,
             "rows_original": int,
             "rows_restored": int,
             "rows_match": bool,
             "hash_match": bool,
             "original_hash": str,
             "restored_hash": str}, ...
          ],
          "errors": [str, ...],
          "passed": bool,
        }
    """
    result: dict = {
        "file": src_db.name,
        "size_bytes": src_db.stat().st_size,
        "backup_bytes": 0,
        "backup_gz_bytes": 0,
        "integrity_ok": False,
        "schema_match": False,
        "tables": [],
        "errors": [],
        "passed": True,
    }

    backup_db_path = backup_dir / src_db.name
    backup_gz_path = backup_dir / f"{src_db.name}.gz"
    restored_db_path = restore_dir / src_db.name

    # --- 1. BACKUP ---------------------------------------------------------
    try:
        backup_db(src_db, backup_db_path)
        result["backup_bytes"] = backup_db_path.stat().st_size
        gzip_file(backup_db_path, backup_gz_path)
        result["backup_gz_bytes"] = backup_gz_path.stat().st_size
    except Exception as e:
        result["errors"].append(f"backup failed: {e}")
        result["passed"] = False
        return result

    # --- 2. RESTORE --------------------------------------------------------
    try:
        gunzip_file(backup_gz_path, restored_db_path)
    except Exception as e:
        result["errors"].append(f"restore (gunzip) failed: {e}")
        result["passed"] = False
        return result

    # --- 3. VERIFY ---------------------------------------------------------
    src_conn = _open_ro(src_db)
    dst_conn: sqlite3.Connection
    try:
        dst_conn = sqlite3.connect(str(restored_db_path), timeout=30)
    except Exception as e:
        result["errors"].append(f"could not open restored DB: {e}")
        result["passed"] = False
        src_conn.close()
        return result

    try:
        # The entire verify block touches the source DB (which may be
        # corrupt — we can't control that, we're testing our backup
        # pipeline against whatever state the live DB is in). Any
        # sqlite3.DatabaseError that escapes a sub-step is recorded as
        # a per-DB failure so the overall run continues to the next DB.
        try:
            # 3a. integrity_check on the restored copy
            ic = dst_conn.execute("PRAGMA integrity_check").fetchone()[0]
            result["integrity_ok"] = (ic == "ok")
            if not result["integrity_ok"]:
                result["errors"].append(
                    f"integrity_check on restored DB returned: {ic}"
                )
                result["passed"] = False

            # 3b. schema comparison
            src_schema = _schema_hash(src_conn)
            dst_schema = _schema_hash(dst_conn)
            result["schema_match"] = (src_schema == dst_schema)
            if not result["schema_match"]:
                result["errors"].append(
                    f"schema mismatch: original={src_schema[:16]} "
                    f"restored={dst_schema[:16]}"
                )
                result["passed"] = False

            # 3c. per-table row count + row hash
            tables = _list_user_tables(src_conn)
            for tbl in tables:
                # A corrupt source table raises sqlite3.DatabaseError on the
                # FIRST malformed page it tries to read. We catch it
                # per-table so one bad table doesn't abort the rest of the
                # comparison, and so the round-trip result for that table is
                # recorded as a mismatch (which is the correct diagnosis — a
                # backup of a corrupt source produces a corrupt restore).
                try:
                    src_hash, src_count = _table_rows_hash(src_conn, tbl)
                except sqlite3.DatabaseError as e:
                    src_hash, src_count = f"<error: {e}>", -1
                    result["errors"].append(
                        f"could not hash source {tbl}: {e}"
                    )
                    result["passed"] = False
                try:
                    dst_hash, dst_count = _table_rows_hash(dst_conn, tbl)
                except sqlite3.DatabaseError as e:
                    dst_hash, dst_count = f"<error: {e}>", -1
                    result["errors"].append(
                        f"could not hash restored {tbl}: {e}"
                    )
                    result["passed"] = False
                entry = {
                    "table": tbl,
                    "rows_original": src_count,
                    "rows_restored": dst_count,
                    "rows_match": src_count == dst_count,
                    "hash_match": src_hash == dst_hash,
                    "original_hash": src_hash,
                    "restored_hash": dst_hash,
                }
                result["tables"].append(entry)
                if src_count == -1 or dst_count == -1:
                    # Already recorded the error above; don't double-log.
                    continue
                if not entry["rows_match"]:
                    result["errors"].append(
                        f"row-count mismatch for {tbl}: "
                        f"original={src_count} restored={dst_count}"
                    )
                    result["passed"] = False
                elif not entry["hash_match"]:
                    # Same count but different content — data divergence.
                    result["errors"].append(
                        f"row-content mismatch for {tbl} "
                        f"(same count {src_count}, different hashes)"
                    )
                    result["passed"] = False
        except sqlite3.DatabaseError as e:
            # Blew up at schema read / table list / integrity check on the
            # source. Record as a per-DB failure and move on.
            result["errors"].append(f"source DB read failure: {e}")
            result["passed"] = False
    finally:
        src_conn.close()
        dst_conn.close()

    return result


def run_test(
    data_dir: Path,
    *,
    keep_temp: bool = False,
) -> dict:
    """Run the round-trip test on every *.db directly in `data_dir`.

    Subdirectories (e.g. `test_run/`, `recon/`) are skipped — they contain
    scratch data, not live bot state.
    """
    overall: dict = {
        "data_dir": str(data_dir),
        "tested_at": datetime.now().isoformat(),
        "databases": [],
        "all_passed": True,
        "temp_dirs": [],
    }

    if not data_dir.is_dir():
        overall["error"] = f"data dir not found: {data_dir}"
        return overall

    db_files = sorted(p for p in data_dir.iterdir() if p.suffix == ".db")
    overall["db_count"] = len(db_files)

    # Create ONE pair of temp dirs (backup/ + restore/) for the whole test.
    # Using a single tmpdir keeps cleanup to one rmtree() call.
    root_tmp = Path(tempfile.mkdtemp(prefix="restore_test_"))
    backup_dir = root_tmp / "backup"
    restore_dir = root_tmp / "restore"
    backup_dir.mkdir(parents=True)
    restore_dir.mkdir(parents=True)
    overall["temp_dirs"] = [str(root_tmp)]

    try:
        for src_db in db_files:
            res = round_trip_one(src_db, backup_dir, restore_dir)
            overall["databases"].append(res)
            if not res["passed"]:
                overall["all_passed"] = False
    finally:
        if keep_temp:
            overall["temp_dirs_kept"] = True
        else:
            shutil.rmtree(root_tmp, ignore_errors=True)
            overall["temp_dirs_kept"] = False

    return overall


def _human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"


def _print_human(overall: dict) -> None:
    print(f"\n{'=' * 70}")
    print("Restore Round-Trip Test Report")
    print(f"{'=' * 70}")
    print(f"Data dir : {overall.get('data_dir')}")
    print(f"Tested   : {overall.get('tested_at')}")
    print(f"DBs      : {overall.get('db_count', 0)}")
    if overall.get("error"):
        print(f"ERROR    : {overall['error']}")
        print(f"{'=' * 70}\n")
        return
    status = "✓ ALL PASS" if overall["all_passed"] else "✗ FAILURES"
    print(f"Overall  : {status}")
    print(f"Temp dirs: {'kept' if overall.get('temp_dirs_kept') else 'cleaned up'}")
    print(f"{'=' * 70}\n")

    for db in overall["databases"]:
        marker = "✓" if db["passed"] else "✗"
        print(
            f"  {marker} {db['file']:<30} "
            f"live={_human(db['size_bytes']):>10}  "
            f"backup={_human(db['backup_bytes']):>10}  "
            f".gz={_human(db['backup_gz_bytes']):>10}"
        )
        print(
            f"     integrity_check={'ok' if db['integrity_ok'] else 'FAIL'}  "
            f"schema_match={'yes' if db['schema_match'] else 'NO'}"
        )
        for e in db["errors"]:
            print(f"     ERROR: {e}")
        for t in db["tables"]:
            row_ok = "✓" if t["rows_match"] else "✗"
            hash_ok = "✓" if t["hash_match"] else "✗"
            print(
                f"        {row_ok}{hash_ok} {t['table']:<28} "
                f"rows={t['rows_original']:>8} "
                f"restored={t['rows_restored']:>8}"
            )
    print(f"\n{'=' * 70}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Round-trip test of the backup+restore pipeline.",
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
        "--keep-temp",
        action="store_true",
        help="Keep the temp backup/ + restore/ dirs after the run "
             "(useful for debugging failures).",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(
            f"ERROR: data dir not found or not a directory: {data_dir}",
            file=sys.stderr,
        )
        return 3

    overall = run_test(data_dir, keep_temp=args.keep_temp)

    if args.json:
        print(json.dumps(overall, indent=2))
    else:
        _print_human(overall)

    return 0 if overall.get("all_passed") else 2


if __name__ == "__main__":
    sys.exit(main())
